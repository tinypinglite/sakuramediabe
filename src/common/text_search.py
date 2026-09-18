"""本地目录搜索的关键词解析：拆词、去重与长度/数量校验。

影片与演员两个列表接口共用同一套词法，保证两端语义一致。
"""

from src.api.exception.errors import ApiError

SEARCH_TERM_MAX_LENGTH = 64
SEARCH_TERM_MAX_COUNT = 6


def split_search_terms(value: str | None, *, error_code: str) -> list[str]:
    """把搜索输入按空白拆成检索词；空输入返回空列表。

    超出词数或词长上限抛 422，``error_code`` 由调用方传入所在接口的错误码。
    """
    if value is None:
        return []
    terms = value.split()
    if not terms:
        return []
    if len(terms) > SEARCH_TERM_MAX_COUNT:
        raise ApiError(422, error_code, "搜索关键词过多", {"query": value})
    for term in terms:
        if len(term) > SEARCH_TERM_MAX_LENGTH:
            raise ApiError(422, error_code, "搜索关键词过长", {"query": value})
    # 去重同时保持输入顺序，避免重复词让相关度分数翻倍。
    return list(dict.fromkeys(terms))
