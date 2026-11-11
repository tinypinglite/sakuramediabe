import ast
import json
from pathlib import Path
from string import Formatter

from src.i18n.catalog import CATALOG_DIR, _select_value, translate
from src.i18n.locale import SUPPORTED_LANGUAGES
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY


def _load_catalog(filename: str) -> dict:
    return json.loads((CATALOG_DIR / filename).read_text(encoding="utf-8"))


def _placeholders(value) -> set[str]:
    values = value.values() if isinstance(value, dict) else [value]
    result: set[str] = set()
    for text in values:
        if not isinstance(text, str):
            continue
        for _, field_name, _, _ in Formatter().parse(text):
            if field_name:
                result.add(field_name)
    return result


def _source_files() -> list[Path]:
    return sorted(
        path for path in Path("src").rglob("*.py") if "__pycache__" not in path.parts
    )


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _provider_operation_error_codes() -> set[str]:
    """取 ProviderOperationError.code 的 Literal 集合，拼成宿主错误码。

    宿主按 ``f"provider_{exc.code}"`` 抛错，静态扫描拿不到具体取值，
    这里回落到协议定义里声明的全量取值。
    """
    path = Path("src/plugins/provider_protocol.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ProviderOperationError":
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if not isinstance(item.target, ast.Name) or item.target.id != "code":
                continue
            annotation = item.annotation
            if not isinstance(annotation, ast.Subscript):
                continue
            if not isinstance(annotation.value, ast.Name) or annotation.value.id != "Literal":
                continue
            elements = (
                annotation.slice.elts
                if isinstance(annotation.slice, ast.Tuple)
                else [annotation.slice]
            )
            return {
                value
                for value in (_string_value(element) for element in elements)
                if value is not None
            }
    return set()


def _function_defs() -> list[ast.FunctionDef]:
    functions: list[ast.FunctionDef] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return functions


def _param_positions(node: ast.FunctionDef) -> dict[str, int | None]:
    """参数名 -> 位置参数下标；仅关键字参数为 None。"""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    positions = {
        argument.arg: index for index, argument in enumerate(positional)
    }
    for argument in args.kwonlyargs:
        positions.setdefault(argument.arg, None)
    return positions


def _direct_code_params(node: ast.FunctionDef) -> set[str]:
    """函数体里直接作为 ApiError 错误码的参数名。"""
    positions = _param_positions(node)
    params: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or _call_name(child) != "ApiError":
            continue
        if len(child.args) < 2 or not isinstance(child.args[1], ast.Name):
            continue
        name = child.args[1].id
        if name in positions:
            params.add(name)
    return params


def _forwarded_code_params(
    node: ast.FunctionDef, carriers: dict[str, dict[str, int | None]]
) -> set[str]:
    """函数体里转发给已知抛出方的错误码参数名。"""
    params: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        callee_slots = carriers.get(_call_name(child) or "")
        if not callee_slots:
            continue
        for keyword in child.keywords:
            if keyword.arg not in callee_slots:
                continue
            if any(
                isinstance(part, ast.Name) and part.id == keyword.arg
                for part in ast.walk(keyword.value)
            ):
                params.add(keyword.arg)
    return params


def _error_code_carriers() -> tuple[dict[str, dict[str, int | None]], set[str]]:
    """找出把某个参数当作 ApiError 错误码抛出的辅助函数。

    返回 ``(函数名 -> 错误码参数名 -> 位置下标, 这些错误码参数的字符串默认值)``。

    - 直接抛出：函数体里 ``ApiError(status, <参数名>, ...)``；
    - 间接抛出：函数体里把错误码参数转发给另一个已知的抛出方
      （如 ``paginate`` → ``validate_page``、``require_by_id`` → ``require_record``）。

    形如 ``ThumbnailDeferred(error_code=...)`` 的业务字段不抛 ApiError，
    因此不在集合内，避免把非错误码的内部标记当成宿主错误码。
    """
    functions = _function_defs()
    carriers: dict[str, dict[str, int | None]] = {}
    changed = True
    while changed:
        changed = False
        for node in functions:
            positions = _param_positions(node)
            params = _direct_code_params(node) | _forwarded_code_params(
                node, carriers
            )
            if not params:
                continue
            slots = {
                param: positions.get(param) for param in params if param in positions
            }
            if node.name in carriers and carriers[node.name] == slots:
                continue
            carriers[node.name] = slots
            changed = True

    defaults: set[str] = set()
    for node in functions:
        slots = carriers.get(node.name)
        if not slots:
            continue
        args = node.args
        pairs = (
            list(zip([*args.posonlyargs, *args.args][-len(args.defaults) :], args.defaults))
            if args.defaults
            else []
        )
        pairs += [
            (argument, value)
            for argument, value in zip(args.kwonlyargs, args.kw_defaults)
            if value is not None
        ]
        for argument, value in pairs:
            if argument.arg not in slots:
                continue
            literal = _string_value(value)
            if literal is not None:
                defaults.add(literal)
    return carriers, defaults


def _require_by_id_codes(node: ast.Call) -> set[str]:
    """require_by_id 的第三个位置参数是 entity_name，派生 ``<entity>_not_found``。"""
    codes: set[str] = set()
    entity: str | None = None
    if len(node.args) >= 3:
        entity = _string_value(node.args[2])
    explicit: str | None = None
    for keyword in node.keywords:
        if keyword.arg == "entity_name":
            entity = _string_value(keyword.value)
        elif keyword.arg == "error_code":
            explicit = _string_value(keyword.value)
    if explicit is not None:
        codes.add(explicit)
    elif entity is not None:
        codes.add(f"{entity}_not_found")
    return codes


def _fstring_prefix(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            break
        parts.append(value.value)
    return "".join(parts)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """模块级字符串常量（``CODE = "code"``），供 ApiError 直接引用。"""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = _string_value(node.value)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = _string_value(node.value)
            if value is None:
                continue
            if isinstance(node.target, ast.Name):
                constants[node.target.id] = value
    return constants


def _error_code_attribute_codes() -> set[str]:
    """收集「把错误码存成属性」的异常在构造点上用到的字符串。

    宿主用 ``ApiError(status, exc.error_code, ...)`` 转发时，错误码藏在属性里，
    静态扫描拿不到具体取值。这里先记住异常 ``__init__`` 里
    ``self.error_code = <参数>`` 的参数位置，再回落到
    ``XxxError(status, "<code>", ...)`` 的构造点上。
    """
    slots: dict[str, str] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                    continue
                positional = [*item.args.posonlyargs, *item.args.args]
                for stmt in ast.walk(item):
                    if not isinstance(stmt, ast.Assign):
                        continue
                    if not isinstance(stmt.value, ast.Name):
                        continue
                    for target in stmt.targets:
                        if not isinstance(target, ast.Attribute):
                            continue
                        if target.attr != "error_code":
                            continue
                        slots[node.name] = stmt.value.id
    codes: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            parameter = slots.get(name or "")
            if parameter is None:
                continue
            for item in ast.walk(tree):
                if not isinstance(item, ast.ClassDef) or item.name != name:
                    continue
                for function in item.body:
                    if not isinstance(function, ast.FunctionDef):
                        continue
                    if function.name != "__init__":
                        continue
                    positional = [
                        argument.arg
                        for argument in [
                            *function.args.posonlyargs,
                            *function.args.args,
                        ]
                        if argument.arg != "self"
                    ]
                    if parameter not in positional:
                        continue
                    index = positional.index(parameter)
                    if len(node.args) <= index:
                        continue
                    literal = _string_value(node.args[index])
                    if literal is not None:
                        codes.add(literal)
    return codes


def _global_string_constants() -> dict[str, set[str]]:
    """全仓模块级字符串常量；同名常量取值不一致时保留全部取值。"""
    values: dict[str, set[str]] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, value in _module_constants(tree).items():
            values.setdefault(name, set()).add(value)
    return values


def _parameter_names(tree: ast.Module) -> set[str]:
    """文件里出现过的形参名：辅助函数把错误码当参数转发时用得上。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            names.add(argument.arg)
        if arguments.vararg is not None:
            names.add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            names.add(arguments.kwarg.arg)
    return names


def _scan_host_error_codes() -> tuple[set[str], set[str]]:
    """扫描宿主可能抛出的错误码。

    返回 ``(错误码集合, 无法静态解析的 f-string 错误码集合)``；后者非空说明
    出现了新的动态错误码构造方式，必须显式处理而不是静默放过。
    """
    provider_codes = _provider_operation_error_codes()
    attribute_codes = _error_code_attribute_codes()
    global_constants = _global_string_constants()
    carriers, carrier_defaults = _error_code_carriers()
    codes: set[str] = set()
    unresolved: set[str] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        parameters = _parameter_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name == "ApiError" and len(node.args) >= 2:
                code = node.args[1]
                literal = _string_value(code)
                if literal is not None:
                    codes.add(literal)
                elif isinstance(code, ast.JoinedStr):
                    if _fstring_prefix(code).startswith("provider_"):
                        codes |= {
                            f"provider_{value}" for value in provider_codes
                        }
                    else:
                        unresolved.add(ast.unparse(code))
                elif isinstance(code, ast.Name):
                    if code.id in parameters:
                        # 辅助函数把错误码当形参转发，码由调用点的字面量决定，
                        # 由 _error_code_carriers 负责收集，这里不重复解析。
                        pass
                    else:
                        # 模块级常量：``ApiError(409, SOME_ERROR_CODE, ...)``。
                        resolved = constants.get(code.id)
                        if resolved is None:
                            candidates = global_constants.get(code.id, set())
                            if len(candidates) == 1:
                                resolved = next(iter(candidates))
                        if resolved is None:
                            unresolved.add(ast.unparse(code))
                        else:
                            codes.add(resolved)
                elif isinstance(code, ast.Attribute) and code.attr == "error_code":
                    # 异常把错误码存成属性再转发：``ApiError(status, exc.error_code, ...)``。
                    codes |= attribute_codes
                else:
                    unresolved.add(ast.unparse(code))
            if name == "require_by_id":
                codes |= _require_by_id_codes(node)
            slots = carriers.get(name)
            if slots:
                for param, position in slots.items():
                    if position is not None:
                        if position >= len(node.args):
                            continue
                        literal = _string_value(node.args[position])
                    else:
                        literal = None
                        for keyword in node.keywords:
                            if keyword.arg == param:
                                literal = _string_value(keyword.value)
                                break
                    if literal is not None:
                        codes.add(literal)
    codes |= carrier_defaults
    return codes, unresolved


def test_every_api_error_code_has_english_translation():
    en = _load_catalog("en.json")
    codes, unresolved = _scan_host_error_codes()
    # 扫不到任何码说明扫描规则失效（源码结构变了），必须让测试失败。
    assert codes, "未扫描到任何宿主错误码，扫描规则可能已失效"
    assert not unresolved, unresolved
    missing = sorted(code for code in codes if f"errors.{code}" not in en)
    assert not missing, missing


def test_catalogs_keep_key_and_placeholder_parity():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")

    # zh-CN 是任务/通知的源文案；en 可以多出仅错误使用的 errors.*。
    assert set(zh) <= set(en)
    assert all(key.startswith("errors.") for key in set(en) - set(zh))
    for key in set(zh):
        assert _placeholders(zh[key]) == _placeholders(en[key]), key


def test_every_registered_task_name_has_both_catalogs():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")

    for task_key in TASK_NAME_REGISTRY:
        key = f"tasks.{task_key}.name"
        assert key in zh, task_key
        assert key in en, task_key


def test_host_notification_keys_exist_in_both_catalogs():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")
    required = {
        "notifications.task_failed.title",
        "notifications.task_failed.content",
        "notifications.task_completed.title",
        "notifications.task_completed.content",
        "notifications.new_media.title",
        "notifications.new_media.content",
    }

    assert required <= set(zh)
    assert required <= set(en)


def test_new_media_notification_plural():
    assert (
        translate("notifications.new_media.content", locale="en", count=1)
        == "1 new movie added"
    )
    assert (
        translate("notifications.new_media.content", locale="en", count=3)
        == "3 new movies added"
    )
    assert (
        translate("notifications.new_media.content", locale="zh-CN", count=3)
        == "新增了 3 个影片"
    )


def test_zh_cn_sample_text_matches_existing_code_strings():
    zh = _load_catalog("zh-CN.json")

    assert zh["notifications.task_failed.title"] == "{task_name}执行失败"
    assert zh["notifications.task_failed.content"] == "后台任务执行失败"
    assert zh["notifications.task_completed.title"] == "{task_name}已完成"
    assert zh["notifications.task_completed.content"] == "后台任务已完成"
    assert zh["tasks.movie_heat_update.name"] == "影片热度更新"


def test_plural_selection_uses_count():
    assert _select_value({"one": "one", "other": "other"}, {"count": 1}) == "one"
    assert _select_value({"one": "one", "other": "other"}, {"count": 0}) == "other"
    assert _select_value({"one": "one", "other": "other"}, {"count": 2}) == "other"
    assert _select_value("plain", {}) == "plain"


def test_translate_interpolates_params():
    assert (
        translate("errors.invalid_language", locale="en", language="fr")
        == "Unsupported language: fr"
    )
    assert (
        translate("notifications.task_failed.title", locale="en", task_name="Sync")
        == "Sync failed"
    )


def test_translate_falls_back_to_default_language_for_tasks():
    assert (
        translate(
            "tasks.movie_heat_update.name",
            locale="en",
        )
        == "Movie heat update"
    )


def test_translate_missing_key_returns_default_or_key():
    assert translate("missing.key", locale="en", default="Fallback") == "Fallback"
    assert translate("missing.key", locale="en") == "missing.key"


def test_translate_does_not_fall_back_to_chinese_for_errors():
    # zh-CN 的错误不使用 catalog；错误 handler 会显式走状态兜底。
    assert "errors.invalid_language" in _load_catalog("en.json")
    assert "errors.invalid_language" not in _load_catalog("zh-CN.json")
    assert (
        translate(
            "errors.invalid_language",
            locale="zh-CN",
            fallback_to_default=False,
            language="fr",
        )
        == "errors.invalid_language"
    )


def _literal_catalog_keys() -> set[str]:
    keys: set[str] = set()
    for path in Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) in {"translate", "translate_for_display"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
            for keyword in node.keywords:
                if keyword.arg in {"text_key", "message_key"}:
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        keys.add(value.value)
    return keys


def test_every_literal_catalog_key_exists():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")
    keys = _literal_catalog_keys()
    assert keys
    assert sorted(key for key in keys if key not in en) == []
    # zh-CN 的 errors.* 只覆盖 handler 兜底用的少量 key，其余错误继续用源码文本。
    assert sorted(
        key for key in keys if key not in zh and not key.startswith("errors.")
    ) == []


def test_every_supported_language_has_catalog():
    missing = [
        language
        for language in SUPPORTED_LANGUAGES
        if not (CATALOG_DIR / f"{language}.json").is_file()
    ]
    assert missing == []
