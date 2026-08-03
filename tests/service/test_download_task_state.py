import pytest

from src.service.transfers.shared.common import is_download_complete, map_download_state


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
