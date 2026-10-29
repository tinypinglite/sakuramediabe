from datetime import datetime, timedelta
from types import SimpleNamespace

from src.schema.transfers.download_selection import (
    DownloadSelectionKeywordGroup,
    DownloadSelectionRanking,
    DownloadSelectionRules,
)
from src.service.transfers.downloads.selection.evaluator import (
    evaluate_and_rank,
    evaluate_candidate,
    normalize_match_text,
)

NOW = datetime(2026, 10, 30, 12, 0, 0)
GIB = 1024**3


def _candidate(
    title: str,
    *,
    description: str = "",
    size_gib: int = 5,
    seeders: int = 10,
    indexer_kind: str = "pt",
    indexer_name: str = "indexer-a",
    published_at: datetime | None = None,
):
    return SimpleNamespace(
        source_uri=f"magnet:?xt={title}",
        indexer_name=indexer_name,
        indexer_kind=indexer_kind,
        movie_number="ABC-001",
        title=title,
        release_title=title,
        description=description,
        published_at=published_at,
        size_bytes=size_gib * GIB,
        seeders=seeders,
    )


def test_normalize_match_text_handles_case_width_and_separators():
    assert normalize_match_text("  ＡＢＣ_１０８０p  ") == "abc 1080p"
    assert normalize_match_text("foo-bar.baz/qux") == "foo bar baz qux"


def test_ascii_terms_require_token_boundaries_but_cjk_uses_substring():
    rules = DownloadSelectionRules(
        required_groups=[
            DownloadSelectionKeywordGroup(label="ai", terms=["AI"]),
            DownloadSelectionKeywordGroup(label="zh", terms=["破解"]),
        ]
    )

    matched = evaluate_candidate(
        _candidate("ABC-001 AI-破解", description="uncensored"),
        rules,
        now=NOW,
    )
    false_positive = evaluate_candidate(
        _candidate("ABC-001 FAITH-破解"),
        rules,
        now=NOW,
    )

    assert matched.eligible is True
    assert false_positive.eligible is False
    assert [reason.code for reason in false_positive.rejection_reasons] == [
        "required_group_missing"
    ]


def test_required_groups_are_and_terms_inside_groups_are_or():
    rules = DownloadSelectionRules(
        required_groups=[
            DownloadSelectionKeywordGroup(label="quality", terms=["4K", "2160p"]),
            DownloadSelectionKeywordGroup(label="source", terms=["WEB-DL", "BluRay"]),
        ]
    )

    assert evaluate_candidate(
        _candidate("ABC-001 2160p WEB-DL"),
        rules,
        now=NOW,
    ).eligible
    result = evaluate_candidate(
        _candidate("ABC-001 2160p CAM"),
        rules,
        now=NOW,
    )
    assert [reason.group_label for reason in result.rejection_reasons] == ["source"]


def test_excluded_terms_reject_even_when_required_and_preferred_match():
    rules = DownloadSelectionRules(
        required_groups=[
            DownloadSelectionKeywordGroup(label="required", terms=["1080p"])
        ],
        excluded_terms=["sample"],
        preferred_groups=[
            DownloadSelectionKeywordGroup(label="preferred", terms=["1080p"])
        ],
    )

    result = evaluate_candidate(
        _candidate("ABC-001 1080p SAMPLE"),
        rules,
        now=NOW,
    )

    assert result.eligible is False
    assert result.matched_preferred_group_label == "preferred"
    assert [reason.code for reason in result.rejection_reasons] == ["excluded_term"]


def test_size_seeders_and_missing_publish_date_are_hard_filters():
    rules = DownloadSelectionRules(
        min_size_bytes=2 * GIB,
        max_size_bytes=10 * GIB,
        min_seeders=3,
        max_age_days=30,
    )

    result = evaluate_candidate(
        _candidate("ABC-001", size_gib=1, seeders=1),
        rules,
        now=NOW,
    )

    assert {reason.code for reason in result.rejection_reasons} == {
        "size_below_min",
        "seeders_below_min",
        "published_at_missing",
    }


def test_publish_date_rule_accepts_recent_and_rejects_old():
    rules = DownloadSelectionRules(max_age_days=7)

    assert evaluate_candidate(
        _candidate("ABC-001", published_at=NOW - timedelta(days=3)),
        rules,
        now=NOW,
    ).eligible
    result = evaluate_candidate(
        _candidate("ABC-001", published_at=NOW - timedelta(days=8)),
        rules,
        now=NOW,
    )
    assert [reason.code for reason in result.rejection_reasons] == [
        "published_at_too_old"
    ]


def test_indexer_scope_and_preferred_group_rank_before_size():
    rules = DownloadSelectionRules(
        indexer_kinds=["pt"],
        indexer_names=["indexer-a", "indexer-b"],
        preferred_groups=[
            DownloadSelectionKeywordGroup(label="target", terms=["target"])
        ],
        ranking=[DownloadSelectionRanking.SIZE_DESC],
    )
    target = _candidate(
        "ABC-001 target",
        size_gib=3,
        indexer_name="indexer-b",
    )
    larger = _candidate(
        "ABC-001 ordinary",
        size_gib=20,
        indexer_name="indexer-a",
    )
    wrong_kind = _candidate(
        "ABC-001 target",
        indexer_name="indexer-a",
        indexer_kind="bt",
    )

    result = evaluate_and_rank([larger, target, wrong_kind], rules, now=NOW)

    assert result.ranked_eligible == [target, larger]
    assert result.evaluations[id(wrong_kind)].eligible is False
    assert result.evaluations[id(target)].selection_rank == 1


def test_configured_ranking_and_stable_ties_are_deterministic():
    rules = DownloadSelectionRules(
        ranking=[
            DownloadSelectionRanking.SEEDERS_DESC,
            DownloadSelectionRanking.PUBLISHED_AT_DESC,
            DownloadSelectionRanking.SIZE_DESC,
        ]
    )
    fewer_newer = _candidate(
        "ABC-001 A",
        seeders=2,
        published_at=NOW,
        size_gib=1,
    )
    more_older = _candidate(
        "ABC-001 B",
        seeders=5,
        published_at=NOW - timedelta(days=10),
        size_gib=10,
    )

    result = evaluate_and_rank([fewer_newer, more_older], rules, now=NOW)

    assert result.ranked_eligible == [more_older, fewer_newer]


def test_indexer_priority_uses_configured_order_then_unlisted_names():
    rules = DownloadSelectionRules(
        ranking=[DownloadSelectionRanking.INDEXER_PRIORITY],
        indexer_priority_names=["site-b", "site-a"],
    )
    site_a = _candidate("ABC-001 A", indexer_name="site-a")
    site_b = _candidate("ABC-001 B", indexer_name="site-b")
    site_c = _candidate("ABC-001 C", indexer_name="site-c")

    result = evaluate_and_rank([site_c, site_a, site_b], rules, now=NOW)

    assert result.ranked_eligible == [site_b, site_a, site_c]
