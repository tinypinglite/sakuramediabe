import pytest

from src.api.exception.errors import ApiError
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.downloads.guards.torrent_content_guard import (
    TorrentInspection,
    assert_candidate_content_importable,
    collect_distinct_movie_numbers,
    content_movie_numbers_match,
)
from src.service.transfers.downloads.search_service import DownloadSearchService


def _candidate(title: str, movie_number: str = "JOB-033") -> DownloadCandidateResource:
    return DownloadCandidateResource(
        source="torznab",
        indexer_name="test-indexer",
        indexer_kind="bt",
        resolved_client_id=1,
        resolved_client_name="qb",
        resolved_client_kind="qbittorrent",
        download_clients=[],
        movie_number=movie_number,
        title=title,
        size_bytes=5 * 1024**3,
        seeders=10,
    )


def test_title_filter_drops_mismatched_and_keeps_unparseable():
    candidates = [
        _candidate("JOB-033 中字"),
        _candidate("CJOB-033 THE BEST OF ... cjob00033"),
        _candidate("高清合集 无码"),
    ]

    result = DownloadSearchService._filter_title_mismatched_candidates(candidates, "JOB-033")

    assert [item.title for item in result] == ["JOB-033 中字", "高清合集 无码"]


def test_title_filter_is_case_insensitive():
    candidates = [_candidate("job-033 1080p")]

    result = DownloadSearchService._filter_title_mismatched_candidates(candidates, "JOB-033")

    assert len(result) == 1


def test_collect_distinct_movie_numbers_filters_by_suffix_and_size():
    files = [
        ("CJOB-033/cjob00033-1.mp4", 5 * 1024**3),
        ("CJOB-033/cjob00033-2.mp4", 5 * 1024**3),
        ("CJOB-033/sample.mp4", 100 * 1024),
        ("CJOB-033/cover.jpg", 1024),
    ]

    assert collect_distinct_movie_numbers(files) == {"CJOB-033"}


def test_content_movie_numbers_match_is_exact_and_case_insensitive():
    assert content_movie_numbers_match("JOB-033", {"JOB-033"})
    assert content_movie_numbers_match("job-033", {"JOB-033"})
    assert not content_movie_numbers_match("JOB-033", {"CJOB-033"})
    # 一本道与加勒比分隔符形态是不同影片，不能折叠后算同一部。
    assert not content_movie_numbers_match("010115_001", {"010115-001"})
    assert content_movie_numbers_match("010115_001", {"010115_001"})
    # 解析不出番号时保持放行，由导入侧父目录兜底。
    assert content_movie_numbers_match("JOB-033", set())


def test_assert_candidate_content_importable_rejects_movie_number_mismatch(monkeypatch):
    def fake_fetch_torrent_files(torrent_url, *, http_client=None):
        return TorrentInspection(
            info_hash="a" * 40,
            files=[("cjob00033/cjob00033-1.mp4", 5 * 1024**3)],
        )

    monkeypatch.setattr(
        "src.service.transfers.downloads.guards.torrent_content_guard.fetch_torrent_files",
        fake_fetch_torrent_files,
    )

    with pytest.raises(ApiError) as exc_info:
        assert_candidate_content_importable(
            movie_number="JOB-033",
            title="JOB-033",
            torrent_url="http://example.test/torrent.torrent",
            magnet_url="",
        )

    assert exc_info.value.code == "download_candidate_content_rejected"
    assert exc_info.value.details["content_movie_numbers"] == ["CJOB-033"]
