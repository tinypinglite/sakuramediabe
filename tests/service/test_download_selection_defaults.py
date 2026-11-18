from src.schema.transfers.download_selection import DownloadSelectionRanking
from src.service.transfers.downloads.selection.defaults import (
    legacy_selection_rules,
    new_user_selection_rules,
)


def test_legacy_default_preserves_pre_policy_ranking():
    rules = legacy_selection_rules()

    assert rules.min_size_bytes == 1024**3
    assert rules.max_size_bytes == 40 * 1024**3
    assert rules.min_seeders == 1
    assert rules.ranking == [DownloadSelectionRanking.SIZE_DESC]


def test_new_user_default_uses_seeders_and_publish_date_as_tie_breakers():
    rules = new_user_selection_rules()

    assert rules.ranking == [
        DownloadSelectionRanking.SIZE_DESC,
        DownloadSelectionRanking.SEEDERS_DESC,
        DownloadSelectionRanking.PUBLISHED_AT_DESC,
    ]
