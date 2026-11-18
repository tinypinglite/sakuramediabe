#!/usr/bin/env python3
"""从 SakuraMediaBE 宿主源码生成插件 SDK 的 schema 与类型存根。

这是维护者工具，不是插件作者的运行时依赖。它需要读取宿主源码并导入
``src.plugins``，因此必须在宿主仓库的 Python 环境里运行：

    cd /path/to/SakuraMediaBE
    uv run --with mypy python ../SakuraMediaBE-public/plugin-sdk/tools/export_from_host.py \
        --host-root .

生成物：

- ``schema/manifest.schema.json``：``PluginManifest`` 的 JSON Schema
- ``stubs/src/...``：插件作者可用的 ``.pyi`` 类型存根

不要手改生成物。Host API 变更后重新运行本脚本。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STUB_MODULES = (
    "src.plugins.contracts",
    "src.plugins.types",
    "src.plugins.context",
    "src.plugins.provider_protocol",
    "src.plugins.extensions.metadata",
    "src.plugins.extensions.ranking",
    "src.plugins.extensions.reviews",
    "src.plugins.extensions.stats",
    "src.scheduler.contracts",
    "src.schema.catalog.subtitles",
)

SUBTITLE_CLASSES = (
    "SubtitleImportStatus",
    "SubtitleImportResult",
    "SubtitleAsset",
    "SubtitleContent",
    "SubtitleReadError",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host-root",
        required=True,
        type=Path,
        help="SakuraMediaBE 宿主仓库根目录（包含 src/plugins）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="plugin-sdk 输出目录，默认是本脚本所在目录的上一级",
    )
    return parser.parse_args()


def run_stubgen(host_root: Path, output_dir: Path) -> None:
    command = [sys.executable, "-m", "mypy.stubgen"]
    for module in STUB_MODULES:
        command.extend(["-p", module])
    command.extend(["-o", str(output_dir)])
    subprocess.run(command, cwd=host_root, check=True)


def extract_class(source: str, name: str) -> str:
    """提取一个顶层 class 块（包含紧邻的装饰器）。"""
    lines = source.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.startswith(f"class {name}")
        )
    except StopIteration as exc:
        raise ValueError(f"stub 中缺少 class {name}") from exc
    block_start = start
    while block_start > 0 and lines[block_start - 1].startswith("@"):
        block_start -= 1
    block_end = start + 1
    while block_end < len(lines):
        line = lines[block_end]
        if line.startswith(("class ", "@")):
            break
        block_end += 1
    return "\n".join(lines[block_start:block_end]).rstrip()


def write_plugins_init(public_exports: dict[str, str], public_all: list[str], target: Path) -> None:
    grouped: dict[str, list[str]] = {}
    for name, module in public_exports.items():
        grouped.setdefault(module, []).append(name)

    lines: list[str] = []
    for module in sorted(grouped):
        names = ", ".join(f"{name} as {name}" for name in sorted(grouped[module]))
        lines.append(f"from {module} import {names}")
    lines.append("")
    lines.append("__all__ = [")
    lines.extend(f"    {name!r}," for name in public_all)
    lines.append("]")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_i18n_stub(target: Path) -> None:
    target.write_text(
        '''from pathlib import Path
from typing import Any

class PluginI18n:
    plugin_id: str
    def __init__(self, plugin_id: str, root_dir: Path | None = None) -> None: ...
    @property
    def locale(self) -> str: ...
    def t(self, key: str, default: str | None = None, **params: Any) -> str: ...
''',
        encoding="utf-8",
    )


def relax_pydantic_models(text: str) -> str:
    """把 Pydantic 模型字段标成可选，并移除 model_config。

    stubgen 只看到注解，看不到 Pydantic 的默认值；直接使用会要求调用方
    传入 model_config 和所有带默认值的字段。类型检查阶段放宽为可选，
    必填约束仍由宿主运行时校验。
    """
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("class "):
            output.append(line)
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith(("class ", "@")):
            end += 1
        block = lines[index:end]
        if not any(item.strip() == "model_config: Incomplete" for item in block):
            output.extend(block)
            index = end
            continue
        output.append(block[0])
        for item in block[1:]:
            if item.strip() == "model_config: Incomplete":
                continue
            if (
                re.match(r"^    [A-Za-z_][A-Za-z0-9_]*: ", item)
                and " = " not in item
                and not item.rstrip().endswith("...")
            ):
                item = item.rstrip() + " = ..."
            output.append(item)
        index = end
    return "\n".join(output) + "\n"


def patch_types_stub(
    types_stub: Path,
    subtitle_stub: Path,
    target: Path,
) -> None:
    source = types_stub.read_text(encoding="utf-8")
    source = re.sub(
        r"^from src\.schema\.catalog\.subtitles import .*\n",
        "",
        source,
        flags=re.MULTILINE,
    )
    source = re.sub(
        r"^from src\.service\.catalog\.catalog_import_service import .*\n",
        "",
        source,
        flags=re.MULTILINE,
    )
    if "from enum import Enum" not in source:
        source = source.replace(
            "from datetime import datetime\n",
            "from datetime import datetime\nfrom enum import Enum\n",
            1,
        )
    if "from pydantic import BaseModel" not in source:
        source = source.replace(
            "from typing import Any\n",
            "from typing import Any\n\nfrom pydantic import BaseModel\n",
            1,
        )
    if "from _typeshed import Incomplete" not in source:
        source = "from _typeshed import Incomplete\n" + source

    extracted = []
    subtitle_source = subtitle_stub.read_text(encoding="utf-8")
    for name in SUBTITLE_CLASSES:
        block = extract_class(subtitle_source, name).replace("(SchemaModel)", "(BaseModel)")
        extracted.append(block)

    source = source.rstrip() + "\n\n" + "\n\n".join(extracted) + "\n"
    source += "\nclass ImageDownloadError(Exception): ...\n"
    target.write_text(source, encoding="utf-8")


def patch_context_stub(source: Path, target: Path) -> None:
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        "from src.plugins.types import ",
        "from src.plugins.i18n import PluginI18n\nfrom src.plugins.types import ",
        1,
    )
    text = text.replace(
        "    def i18n(self): ...",
        "    def i18n(self) -> PluginI18n: ...",
        1,
    )
    target.write_text(text, encoding="utf-8")


def read_type_aliases(source: Path) -> dict[str, str]:
    pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): TypeAlias = (.+)$")
    aliases: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            aliases[match.group(1)] = match.group(2).strip()
    return aliases


def patch_provider_stub(
    source: Path,
    target: Path,
    aliases: dict[str, str],
) -> None:
    lines = source.read_text(encoding="utf-8").splitlines()
    patched: list[str] = []
    bare_alias = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): TypeAlias$")
    for line in lines:
        match = bare_alias.match(line)
        if match and match.group(1) in aliases:
            line = f"{match.group(1)}: TypeAlias = {aliases[match.group(1)]}"
        patched.append(line)

    joined = "\n".join(patched)
    defined = set(
        re.findall(r"^([A-Za-z_][A-Za-z0-9_]*): TypeAlias", joined, re.MULTILINE)
    )
    missing = [name for name in aliases if name not in defined]
    if missing:
        insert_at = max(
            index
            for index, line in enumerate(patched)
            if line.startswith(("from ", "import "))
        ) + 1
        block = [f"{name}: TypeAlias = {aliases[name]}" for name in missing]
        patched[insert_at:insert_at] = [""] + block

    target.write_text("\n".join(patched) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    host_root = args.host_root.resolve()
    if not (host_root / "src/plugins/manifest.py").is_file():
        raise SystemExit(f"不是有效的宿主仓库: {host_root}")

    out = args.out.resolve()
    (out / "schema").mkdir(parents=True, exist_ok=True)
    stubs_root = out / "stubs"
    if stubs_root.exists():
        shutil.rmtree(stubs_root)
    stubs_root.mkdir(parents=True)

    sys.path.insert(0, str(host_root))
    from src.plugins import _PUBLIC_EXPORTS
    from src.plugins import __all__ as public_all
    from src.plugins.manifest import PluginManifest

    schema = PluginManifest.model_json_schema()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
        "title": "SakuraMedia plugin manifest",
    }
    (out / "schema/manifest.schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with tempfile.TemporaryDirectory(prefix="sakuramedia-sdk-") as raw_tmp:
        tmp = Path(raw_tmp)
        run_stubgen(host_root, tmp)
        generated = tmp / "src"

        plugins_dir = stubs_root / "src/plugins"
        extensions_dir = plugins_dir / "extensions"
        scheduler_dir = stubs_root / "src/scheduler"
        extensions_dir.mkdir(parents=True)
        scheduler_dir.mkdir(parents=True)
        (stubs_root / "src/__init__.pyi").write_text("", encoding="utf-8")

        shutil.copy2(generated / "plugins/contracts.pyi", plugins_dir / "contracts.pyi")
        shutil.copy2(
            generated / "plugins/extensions/metadata.pyi",
            extensions_dir / "metadata.pyi",
        )
        shutil.copy2(
            generated / "plugins/extensions/ranking.pyi",
            extensions_dir / "ranking.pyi",
        )
        shutil.copy2(
            generated / "plugins/extensions/reviews.pyi",
            extensions_dir / "reviews.pyi",
        )
        shutil.copy2(
            generated / "plugins/extensions/stats.pyi",
            extensions_dir / "stats.pyi",
        )
        shutil.copy2(generated / "scheduler/contracts.pyi", scheduler_dir / "contracts.pyi")

        patch_types_stub(
            generated / "plugins/types.pyi",
            generated / "schema/catalog/subtitles.pyi",
            plugins_dir / "types.pyi",
        )
        patch_context_stub(generated / "plugins/context.pyi", plugins_dir / "context.pyi")
        patch_provider_stub(
            generated / "plugins/provider_protocol.pyi",
            plugins_dir / "provider_protocol.pyi",
            read_type_aliases(host_root / "src/plugins/provider_protocol.py"),
        )
        write_i18n_stub(plugins_dir / "i18n.pyi")
        write_plugins_init(_PUBLIC_EXPORTS, public_all, plugins_dir / "__init__.pyi")
        (scheduler_dir / "__init__.pyi").write_text(
            "from src.scheduler.contracts import JobDefinition as JobDefinition\n\n"
            '__all__ = ["JobDefinition"]\n',
            encoding="utf-8",
        )

        for stub_path in stubs_root.rglob("*.pyi"):
            stub_path.write_text(
                relax_pydantic_models(stub_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )

    print(f"已生成 {out / 'schema/manifest.schema.json'}")
    print(f"已生成 {out / 'stubs'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
