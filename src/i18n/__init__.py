"""宿主多语言基础设施：语言协商、请求上下文与 JSON 语言包。"""

from src.i18n.catalog import translate
from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    LanguageState,
    apply_user_language,
    current_language,
    normalize_language,
    parse_accept_language,
    reset_language_state,
    set_language_state,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "LanguageState",
    "apply_user_language",
    "current_language",
    "normalize_language",
    "parse_accept_language",
    "reset_language_state",
    "set_language_state",
    "translate",
]
