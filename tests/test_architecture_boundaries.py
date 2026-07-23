import ast
from pathlib import Path


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_cloud115_sdk_does_not_depend_on_business_layers() -> None:
    forbidden_prefixes = ("src.model", "src.schema", "src.service")
    for path in Path("src/lib/cloud115").rglob("*.py"):
        forbidden = {
            module
            for module in _imported_modules(path)
            if module.startswith(forbidden_prefixes)
        }
        assert not forbidden, f"{path} imports business layers: {sorted(forbidden)}"


def test_rapid_upload_does_not_depend_on_playback_service() -> None:
    for path in Path("src/service/transfers/media_rapid_upload").rglob("*.py"):
        forbidden = {
            module
            for module in _imported_modules(path)
            if module.startswith("src.service.playback")
        }
        assert not forbidden, f"{path} imports playback service: {sorted(forbidden)}"


# 只包含"以兼容目的重导公开名字"的 facade shim；这些文件里可以自由触到迁移后模块的
# 私有别名。真正的 god-class（ActivityService / MediaRapidUploadService / MediaThumbnailService）
# 目前是通过多继承拼装出来的，任何跨域调用方都不应戳它们的 `_` 前缀方法。
GOD_SERVICES = frozenset(
    (
        "ActivityService",
        "MediaThumbnailService",
        "MediaRapidUploadService",
    )
)

_FACADE_SHIMS = frozenset(
    (
        Path("src/service/system/activity_service.py"),
        Path("src/service/playback/media_thumbnail_service.py"),
        Path("src/service/transfers/media_rapid_upload_service.py"),
        Path("src/service/playback/cloud115_backend_service.py"),
    )
)


def _private_god_accesses(tree: ast.AST) -> list[tuple[str, str, int]]:
    """收集所有形如 ``GodClass._foo`` / ``mod.GodClass._foo`` 的属性访问节点。"""
    hits: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # 只关心单下划线约定的私有名，跳过 dunder。
        if not node.attr.startswith("_") or node.attr.startswith("__"):
            continue
        target = node.value
        if isinstance(target, ast.Name) and target.id in GOD_SERVICES:
            hits.append((target.id, node.attr, node.lineno))
        elif isinstance(target, ast.Attribute) and target.attr in GOD_SERVICES:
            # 命中 `module.GodService._x` 这类中转别名。
            hits.append((target.attr, node.attr, node.lineno))
    return hits


def test_cross_domain_callers_do_not_use_god_service_private_methods() -> None:
    violations: list[str] = []
    for path in Path("src").rglob("*.py"):
        if path in _FACADE_SHIMS:
            continue
        tree = ast.parse(path.read_text())
        for cls_name, attr, lineno in _private_god_accesses(tree):
            violations.append(f"{path}:{lineno}: {cls_name}.{attr}")
    assert not violations, "\n".join(violations)
