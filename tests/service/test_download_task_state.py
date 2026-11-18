import pytest

from src.api.exception.errors import ApiError
from src.service.transfers.downloads.common import (
    DOWNLOAD_STATES,
    is_download_complete,
    normalize_state_filters,
)


def test_provider_completed_state_is_eligible_for_import():
    assert is_download_complete("completed") is True
    assert is_download_complete("downloading") is False


def test_normalize_state_filters_none_or_empty_returns_none():
    assert normalize_state_filters(None, field_name="state") is None
    assert normalize_state_filters([], field_name="state") is None
    assert normalize_state_filters([" ", ""], field_name="state") is None


def test_normalize_state_filters_merges_and_deduplicates():
    assert normalize_state_filters(
        ["downloading", "completed", "downloading"], field_name="state"
    ) == {"downloading", "completed"}


def test_normalize_state_filters_rejects_unknown_state():
    with pytest.raises(ApiError) as exc:
        normalize_state_filters(["downloading", "bogus"], field_name="state")

    assert exc.value.status_code == 422
    assert exc.value.code == "invalid_download_task_filter"


def test_provider_state_contract_is_closed():
    assert DOWNLOAD_STATES == {"queued", "downloading", "completed", "failed"}
