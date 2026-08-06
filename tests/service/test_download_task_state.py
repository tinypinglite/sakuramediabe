import pytest

from src.api.exception.errors import ApiError
from src.service.transfers.downloads.common import (
    ALLOWED_DOWNLOAD_STATES,
    is_download_complete,
    map_download_state,
    normalize_state_filters,
)


# 已下完但停止做种的 *UP：归到 completed，且下游判"已完成"应通过，才能继续走自动导入。
@pytest.mark.parametrize("raw_state", ["stoppedUP", "pausedUP"])
def test_stopped_complete_torrents_remain_eligible_for_import(raw_state):
    normalized = map_download_state(raw_state)

    assert normalized == "completed"
    assert is_download_complete(normalized) is True


# 未下完就暂停的 *DL：仍旧是 paused，且下游不应误判为已完成。
@pytest.mark.parametrize("raw_state", ["stoppedDL", "pausedDL"])
def test_stopped_incomplete_torrents_remain_paused(raw_state):
    normalized = map_download_state(raw_state)

    assert normalized == "paused"
    assert is_download_complete(normalized) is False


def test_normalize_state_filters_none_or_empty_returns_none():
    assert (
        normalize_state_filters(
            None, field_name="download_state", allowed_values=ALLOWED_DOWNLOAD_STATES
        )
        is None
    )
    assert (
        normalize_state_filters(
            [], field_name="download_state", allowed_values=ALLOWED_DOWNLOAD_STATES
        )
        is None
    )
    # 全空白项视作不过滤。
    assert (
        normalize_state_filters(
            [" ", ""], field_name="download_state", allowed_values=ALLOWED_DOWNLOAD_STATES
        )
        is None
    )


def test_normalize_state_filters_merges_and_dedups():
    result = normalize_state_filters(
        ["downloading", "stalled", "downloading"],
        field_name="download_state",
        allowed_values=ALLOWED_DOWNLOAD_STATES,
    )

    assert result == {"downloading", "stalled"}


def test_normalize_state_filters_rejects_unknown_state():
    with pytest.raises(ApiError) as exc:
        normalize_state_filters(
            ["downloading", "bogus"],
            field_name="download_state",
            allowed_values=ALLOWED_DOWNLOAD_STATES,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "invalid_download_task_filter"

