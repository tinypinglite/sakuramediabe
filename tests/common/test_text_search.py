import pytest

from src.api.exception.errors import ApiError
from src.common.text_search import (
    SEARCH_TERM_MAX_COUNT,
    SEARCH_TERM_MAX_LENGTH,
    split_search_terms,
)


def test_split_search_terms_splits_dedupes_and_keeps_order():
    assert split_search_terms(
        " 温泉  三上 温泉 ", error_code="invalid_movie_filter"
    ) == ["温泉", "三上"]


def test_split_search_terms_empty_input_returns_empty_list():
    assert split_search_terms(None, error_code="invalid_movie_filter") == []
    assert split_search_terms("   ", error_code="invalid_movie_filter") == []


def test_split_search_terms_rejects_too_many_terms():
    query = " ".join(f"词{index}" for index in range(SEARCH_TERM_MAX_COUNT + 1))
    with pytest.raises(ApiError) as error:
        split_search_terms(query, error_code="invalid_movie_filter")
    assert error.value.status_code == 422
    assert error.value.code == "invalid_movie_filter"


def test_split_search_terms_rejects_overlong_term():
    with pytest.raises(ApiError) as error:
        split_search_terms(
            "a" * (SEARCH_TERM_MAX_LENGTH + 1), error_code="invalid_actor_filter"
        )
    assert error.value.status_code == 422
    assert error.value.code == "invalid_actor_filter"
