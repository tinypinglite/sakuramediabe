"""插件代码 import 白名单静态扫描。

用 AST 扫描插件包内全部 .py 文件，只允许标准库、`src.plugins`（含 types）、
`src.scheduler.contracts` 与相对导入。动态 import（importlib.import_module）
无法静态覆盖，文档注明局限，由同进程可信模型兜底。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from loguru import logger
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from src.plugins.manifest import PluginManifest

# 只放行公开契约面；宿主内部实现（installer/loader/dependencies/manager 等）不允许绑定，
# 避免插件悄悄依赖内部结构导致宿主重构即损坏。
ALLOWED_HOST_MODULES = frozenset(
    {
        "src.plugins",
        "src.plugins.types",
        "src.scheduler.contracts",
    }
)
# 契约依赖：params_schema/settings 模型都基于 pydantic，插件可稳定使用。
ALLOWED_EXTRA_ROOTS = frozenset({"pydantic"})


class ImportBoundaryError(RuntimeError):
    """插件 import 越界；fields 为 (file, line, module) 列表。"""

    def __init__(self, violations: list[tuple[str, int, str]]):
        self.violations = violations
        detail = "; ".join(
            f"{file}:{line} -> {module}" for file, line, module in violations
        )
        super().__init__(f"插件存在越界 import: {detail}")


def _module_allowed(module: str | None) -> bool:
    if module is None:
        # from . import x / from .. import x：相对导入永远放行。
        return True
    if module.startswith("."):
        return True
    return module in ALLOWED_HOST_MODULES


def _plugin_dep_roots(plugin_dir: Path) -> set[str]:
    """插件 deps/ 目录里的顶层包名（pip --target 布局：目录或单 .py 文件）。"""
    deps_dir = plugin_dir / "deps"
    roots: set[str] = set()
    if not deps_dir.is_dir():
        return roots
    for entry in deps_dir.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if entry.name.endswith((".dist-info", ".egg-info")):
                continue
            roots.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            roots.add(entry.stem)
    return roots


def scan_plugin_imports(plugin_dir: Path, manifest: PluginManifest) -> None:
    """扫描插件目录下全部 .py，越界即抛 ImportBoundaryError。"""
    violations: list[tuple[str, int, str]] = []
    allowed_roots = (
        set(sys.stdlib_module_names)
        | set(ALLOWED_EXTRA_ROOTS)
        | _plugin_dep_roots(plugin_dir)
    )
    # 宿主已满足而"复用宿主"的声明依赖也要放行（它们不在插件 deps 目录里）。
    for requirement_str in manifest.dependencies.requirements:
        try:
            name = canonicalize_name(Requirement(requirement_str).name)
        except Exception as exc:
            logger.warning(
                "插件依赖声明无法解析，跳过白名单扩展 requirement={} detail={}",
                requirement_str,
                exc,
            )
            continue
        allowed_roots.add(name)
        allowed_roots.add(name.replace("-", "_"))
    for py_file in sorted(plugin_dir.rglob("*.py")):
        relative = py_file.relative_to(plugin_dir)
        if relative.parts and relative.parts[0] in {"deps", "data", ".staging"}:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (OSError, SyntaxError):
            # 语法错误由后续真实 import 暴露，这里不重复拦截。
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        not _module_allowed(alias.name)
                        and alias.name.split(".", 1)[0] not in allowed_roots
                    ):
                        violations.append((str(relative), node.lineno, alias.name))
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module is not None
                and not _module_allowed(node.module)
                and node.module.split(".", 1)[0] not in allowed_roots
            ):
                violations.append((str(relative), node.lineno, node.module or "."))
    if violations:
        raise ImportBoundaryError(violations)
