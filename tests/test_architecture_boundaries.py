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
    # 秒传实现已迁入 rapid_upload 子包；旧目录已删除，继续扫描会让护栏空跑。
    for path in Path("src/service/transfers/rapid_upload").rglob("*.py"):
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
        Path("src/service/playback/media_thumbnail_service.py"),
        Path("src/service/transfers/rapid_upload/facade.py"),
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
    # 钉住发现逻辑本身：已知 god-class 必须在结果里，否则守卫会静默空跑。
    discovered = {facade.__name__ for facade in facades}
    assert {"ActivityService", "MediaRapidUploadService"} <= discovered, sorted(discovered)

    violations: list[str] = []
    for facade in facades:
        for name, owners in sorted(_ambiguous_names(facade).items()):
            violations.append(
                f"{facade.__module__}.{facade.__name__}: 方法名 {name} 同时定义在 "
                f"{owners}，经门面调用时 cls.{name} 会被 MRO 劫持到 {owners[0]}"
                f"——给它们取互不冲突的名字"
            )
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# 悬空 cls./self. 引用守卫
#
# facade 的各 base 之间靠 `cls.<兄弟类方法>` 互调，脱离门面单独调用就是
# AttributeError。这类类只能同包内被门面组装，一旦被跨包 import 直接调用即崩。
# 同一守卫顺带覆盖另一种形态：重构删掉了某个私有辅助方法，调用点却还在——
# 该类同样会出现解析不到的 `cls.` 引用，而它是被跨包直接使用的入口类。
#
# 历史事故：queue_tasks 直接 import MediaRapidUploadExecutor（秒传任务全崩）。
# ---------------------------------------------------------------------------


def _class_defined_names(node: ast.ClassDef) -> set[str]:
    """类自身提供的名字：方法、类属性，以及方法体里赋值的 self.x。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub in node.body:
            names.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif _is_self_attribute(target):
                    names.add(target.attr)
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            target = sub.target
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif _is_self_attribute(target):
                names.add(target.attr)
    return names


def _is_self_attribute(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _class_self_references(node: ast.ClassDef) -> set[str]:
    return {
        sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id in ("cls", "self")
    }


def _collect_src_classes() -> dict[str, dict]:
    """AST 收集 src 下所有类定义。用 AST 而非 import，避免全量导入 src 的副作用。"""
    collected: dict[str, dict] = {}
    for path in sorted(Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [
                base.id if isinstance(base, ast.Name) else base.attr
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            ]
            collected[f"{path}::{node.name}"] = {
                "name": node.name,
                "path": path,
                "bases": bases,
                "names": _class_defined_names(node),
                "refs": _class_self_references(node),
            }
    return collected


def _classes_with_dangling_refs() -> dict[str, list[str]]:
    """返回「自身 MRO 内解析不到 cls./self. 引用」的类 -> 缺失的名字。

    基类含仓库外的类（Model / BaseModel / Enum …）时静态判不出属性全集，跳过。
    """
    collected = _collect_src_classes()
    by_name: dict[str, list[str]] = {}
    for key, info in collected.items():
        by_name.setdefault(info["name"], []).append(key)

    def inherited(key: str, seen: set[str]) -> tuple[set[str], bool]:
        if key in seen:
            return set(), True
        seen.add(key)
        info = collected[key]
        names = set(info["names"])
        fully_internal = True
        for base in info["bases"]:
            if base == "object":
                continue
            candidates = by_name.get(base)
            if not candidates:
                fully_internal = False
                continue
            for candidate in candidates:
                base_names, base_internal = inherited(candidate, seen)
                names |= base_names
                fully_internal = fully_internal and base_internal
        return names, fully_internal

    dangling: dict[str, list[str]] = {}
    for key, info in collected.items():
        names, fully_internal = inherited(key, set())
        if not fully_internal:
            continue
        missing = sorted(ref for ref in info["refs"] if ref not in names)
        if missing:
            dangling[key] = missing
    return dangling


def test_classes_with_dangling_self_refs_are_not_used_outside_their_package() -> None:
    """依赖兄弟类方法的 base 只能留在同包内供门面组装，不得被跨包 import。"""
    collected = _collect_src_classes()
    dangling = _classes_with_dangling_refs()
    # 钉住检测逻辑：已知的 facade base 必须被识别出来，否则守卫在空跑。
    assert "MediaRapidUploadExecutor" in {
        collected[key]["name"] for key in dangling
    }, sorted(collected[key]["name"] for key in dangling)

    owners: dict[str, list[tuple[Path, list[str]]]] = {}
    for key, missing in dangling.items():
        owners.setdefault(collected[key]["name"], []).append(
            (collected[key]["path"], missing)
        )

    violations: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                for owner_path, missing in owners.get(alias.name, []):
                    if owner_path.parent == path.parent:
                        continue  # 同包内：门面组装的正常形态
                    violations.append(
                        f"{path}:{node.lineno}: 直接 import {alias.name}，但它的 "
                        f"{missing[:3]} 等引用要靠同包兄弟类提供"
                        f"——改用组合后的门面，或把缺失方法补回该类"
                    )
    assert not violations, "\n".join(violations)
