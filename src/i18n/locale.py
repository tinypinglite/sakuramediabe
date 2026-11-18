"""语言标签归一化、Accept-Language 解析与请求级语言上下文。

这里的逻辑保持纯函数/纯上下文，不依赖数据库或 HTTP 框架，便于后台任务和测试复用。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

DEFAULT_LANGUAGE = "zh-CN"
# 非默认语言缺译文时的兜底语言；绝不回退到中文源码。
FALLBACK_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("zh-CN", "en")

# 只接受产品明确支持的语言。zh-TW / zh-Hant 不做兼容折叠，避免把繁体
# 请求静默当成简体处理。
_EXACT_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-sg": "zh-CN",
    "en": "en",
}

_LANGUAGE_BASES = {
    "zh": "zh-CN",
    "en": "en",
}


def normalize_language(value: str | None) -> str | None:
    """把常见 BCP-47 变体归一化为受支持的语言标签；不支持时返回 None。"""
    if not value:
        return None
    normalized = value.strip().replace("_", "-").lower()
    if not normalized:
        return None
    exact = _EXACT_ALIASES.get(normalized)
    if exact is not None:
        return exact
    # en-US / en-GB 等一律折叠到 en；zh 的其他子标签只接受 Hans/CN/SG。
    base = normalized.split("-", 1)[0]
    if base == "en":
        return "en"
    if base == "zh":
        if any(part in normalized.split("-")[1:] for part in ("hans", "cn", "sg")):
            return "zh-CN"
        return None
    return _LANGUAGE_BASES.get(base)


def parse_accept_language(header: str | None) -> str | None:
    """按 q 值从高到低返回第一个受支持的语言；没有合法值时返回 None。"""
    if not header:
        return None
    candidates: list[tuple[float, int, str]] = []
    for index, item in enumerate(header.split(",")):
        parts = [part.strip() for part in item.split(";")]
        if not parts or not parts[0]:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        language = normalize_language(parts[0])
        if language is None:
            continue
        # q 值相同时保持 header 中的原始顺序（-index 越大越靠前）。
        candidates.append((quality, -index, language))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


@dataclass
class LanguageState:
    """请求级语言状态。

    header_language 由中间件写入；user_language 由认证依赖回填。使用可变对象
    是刻意的：FastAPI 的同步依赖会在线程池中执行，ContextVar.set() 的返回值
    不会传播回事件循环，但依赖可以原地修改这个对象。
    """

    header_language: str | None = None
    user_language: str | None = None

    @property
    def effective(self) -> str:
        return self.header_language or self.user_language or DEFAULT_LANGUAGE


_current_language_state: ContextVar[LanguageState | None] = ContextVar(
    "sakuramedia_language_state",
    default=None,
)


def new_language_state(header: str | None = None) -> LanguageState:
    return LanguageState(header_language=parse_accept_language(header))


def set_language_state(state: LanguageState) -> Token:
    return _current_language_state.set(state)


def reset_language_state(token: Token) -> None:
    _current_language_state.reset(token)


def current_language_state() -> LanguageState | None:
    return _current_language_state.get()


def current_language() -> str:
    state = _current_language_state.get()
    if state is None:
        return DEFAULT_LANGUAGE
    return state.effective


def apply_user_language(language: str | None) -> None:
    """认证依赖回填用户偏好；没有请求上下文时是 no-op。"""
    state = _current_language_state.get()
    if state is not None:
        state.user_language = normalize_language(language)
