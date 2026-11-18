"""插件包试加载的隔离子进程。

Nuitka 编译插件在首次导入时会把自己的 finder 挂到 ``sys.meta_path`` 上，并把
插件模块名绑定在首次加载的目录上；宿主清理 ``sys.modules`` 并不能解除这层
绑定。若安装/升级前在宿主进程里试加载新包，导入会命中旧 finder 执行旧版
编译代码，``register()`` 返回的版本与 staging 里的 manifest 不一致。

因此安装与升级前的 ``check_plugin_dir`` 统一放到独立子进程执行：子进程退出
即丢弃全部导入副作用，宿主进程当前加载的插件不受影响。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from src.config.config import Plugins
from src.plugins.loader import PluginLoadError, check_plugin_dir

# Nuitka 编译后的模块没有 runpy 需要的 code object，不能再用 ``python -m``；
# 用 ``-c`` 显式调用入口，参数与 stdin 协议保持不变。
_CHILD_CODE = "from src.plugins.validation import main; raise SystemExit(main())"


def check_plugin_dir_isolated(
    *,
    plugin_dir: Path,
    plugin_settings: Plugins | None = None,
) -> None:
    """在独立子进程中执行 ``check_plugin_dir``；失败按 ``PluginLoadError`` 抛回。"""
    plugin_dir = Path(plugin_dir)
    plugin_id = plugin_dir.name
    payload = json.dumps(
        {
            "plugin_dir": str(plugin_dir),
            "plugins": (plugin_settings or Plugins()).model_dump(mode="json"),
        }
    )
    with tempfile.NamedTemporaryFile(
        prefix="sakuramedia-plugin-check-",
        suffix=".json",
        delete=False,
    ) as result_file:
        result_path = Path(result_file.name)
    try:
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD_CODE, str(result_path)],
                input=payload,
                text=True,
                capture_output=True,
                check=False,
                cwd=_project_root(),
                env=_child_env(),
            )
        except OSError as exc:
            raise PluginLoadError(
                plugin_id, "load", f"无法启动插件校验进程: {exc}"
            ) from exc
        try:
            report = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            details = (completed.stderr or completed.stdout).strip().splitlines()
            suffix = details[-1] if details else f"退出码 {completed.returncode}"
            raise PluginLoadError(
                plugin_id, "load", f"插件校验进程异常退出: {suffix}"
            ) from None
        if report.get("ok") is not True:
            raise PluginLoadError(
                plugin_id,
                str(report.get("stage") or "load"),
                str(report.get("reason") or "插件校验失败"),
            )
    finally:
        result_path.unlink(missing_ok=True)


def _project_root() -> Path:
    """宿主代码根目录（``src`` 的父目录），保证子进程能导入 ``src``。"""
    return Path(__file__).resolve().parents[2]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    root = str(_project_root())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not existing else os.pathsep.join([root, existing])
    return env


def main(argv: list[str] | None = None) -> int:
    """子进程入口：stdin 读请求，结果写入 argv[1] 指定的 JSON 文件。"""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -c <child-code> <result-path>", file=sys.stderr)
        return 2
    result_path = Path(args[0])
    try:
        payload = json.load(sys.stdin)
        check_plugin_dir(
            plugin_dir=Path(payload["plugin_dir"]),
            plugin_settings=Plugins.model_validate(payload["plugins"]),
        )
    except PluginLoadError as exc:
        report = {"ok": False, "stage": exc.stage, "reason": exc.reason}
    except Exception as exc:  # 原样回传给宿主渲染
        report = {"ok": False, "stage": "load", "reason": str(exc)}
    else:
        report = {"ok": True}
    result_path.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
