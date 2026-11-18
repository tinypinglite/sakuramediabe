import ast
import types
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


# 只包含"以兼容目的重导公开名字"的 facade shim；这些文件里可以自由触到迁移后模块的
# 私有别名。ActivityService 通过多继承拼装，任何跨域调用方都不应戳它的 `_` 前缀方法。
GOD_SERVICES = frozenset(
    (
        "ActivityService",
    )
)

_FACADE_SHIMS = frozenset(
    (
        Path("src/service/playback/media_thumbnail_service.py"),
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

# ---------------------------------------------------------------------------
# 多继承 facade 的同名方法守卫
#
# 上面这几个 god-class 是把多个无状态 service 平铺多继承拼出来的。只要两个 base
# 定义了同名方法，base 内部的 `cls.<同名方法>` 经 facade 调用时就会解析到 MRO 更
# 靠前的那一侧——不报错，只在参数对不上时才炸。历史事故：
# NotificationService.list_notifications 里的 `cls.build_query` 打到了
# TaskRunService.build_query，GET /system/notifications 直接 500。
#
# 不变式：facade 的各 base 之间不得出现同名方法。名字不撞，`cls.` 自引用就天然
# 安全，不需要靠"禁止写 cls."这种只能人肉记住的约定。
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    parts = path.with_suffix("").parts
    # 包的 __init__ 归到包本身，避免以 `pkg.__init__` 重复导入同一模块。
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _own_method_names(klass: type) -> set[str]:
    return {
        name
        for name, value in vars(klass).items()
        if not name.startswith("__")
        and isinstance(value, (staticmethod, classmethod, types.FunctionType))
    }


def _internal_multi_base_classes() -> list[type]:
    """收集 service 层「基类不止一个、且全部来自 src.」的类，即内部多继承 facade。

    只认全内部基类：混入第三方基类（Pydantic / Generic 等）的类不属于本守卫的
    关注范围。硬编码 facade 清单会随新 facade 加入而过期，所以这里自动发现。
    """
    import importlib

    classes: list[type] = []
    for path in sorted(Path("src/service").rglob("*.py")):
        tree = ast.parse(path.read_text())
        candidates = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and len(node.bases) >= 2
        ]
        if not candidates:
            continue
        module = importlib.import_module(_module_name(path))
        for name in candidates:
            klass = getattr(module, name, None)
            if not isinstance(klass, type):
                continue
            bases = [base for base in klass.__mro__[1:] if base is not object]
            if len(bases) >= 2 and all(
                base.__module__.startswith("src.") for base in bases
            ):
                classes.append(klass)
    return classes


def _ambiguous_names(facade_cls: type) -> dict[str, list[str]]:
    """返回 facade 各 base 之间同名的方法 -> 定义它的 base 名（按 MRO 顺序）。"""
    owners: dict[str, list[str]] = {}
    for base in facade_cls.__mro__[1:]:
        if base is object:
            continue
        for name in _own_method_names(base):
            owners.setdefault(name, []).append(base.__name__)
    return {name: bases for name, bases in owners.items() if len(bases) > 1}


def test_multi_base_facades_have_no_ambiguous_method_names() -> None:
    facades = _internal_multi_base_classes()
    # 钉住发现逻辑本身：ActivityService 必须在结果里，否则守卫会静默空跑。
    discovered = {facade.__name__ for facade in facades}
    assert "ActivityService" in discovered, sorted(discovered)

    violations: list[str] = []
    for facade in facades:
        for name, owners in sorted(_ambiguous_names(facade).items()):
            violations.append(
                f"{facade.__module__}.{facade.__name__}: 方法名 {name} 同时定义在 "
                f"{owners}，经门面调用时 cls.{name} 会被 MRO 劫持到 {owners[0]}"
                f"——给它们取互不冲突的名字"
            )
    assert not violations, "\n".join(violations)
