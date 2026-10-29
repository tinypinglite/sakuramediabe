"""纯本地的选种规则匹配与排序。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.common.runtime_time import to_db_utc_naive
from src.i18n import translate
from src.schema.transfers.download_selection import (
    DownloadSelectionKeywordGroup,
    DownloadSelectionRanking,
    DownloadSelectionRules,
)
from src.schema.transfers.downloads import (
    DownloadSelectionRejectionResource,
)

MAX_SELECTION_ATTEMPTS = 5

_ASCII_TERM_PATTERN = re.compile(r"^[a-z0-9 ]+$")


def normalize_match_text(value: str | None) -> str:
    """统一全半角、大小写及标点分隔，供标题与简介匹配。"""

    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"[_\W]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _term_matches(text: str, term: str) -> bool:
    if not text or not term:
        return False
    if _ASCII_TERM_PATTERN.fullmatch(term):
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return term in text


def _group_matches(text: str, group: DownloadSelectionKeywordGroup) -> bool:
    return any(_term_matches(text, normalize_match_text(term)) for term in group.terms)


def _candidate_text(candidate: Any, rules: DownloadSelectionRules) -> str:
    parts: list[str] = []
    if rules.match_title:
        parts.append(
            str(
                getattr(candidate, "release_title", "")
                or getattr(candidate, "title", "")
                or ""
            )
        )
    if rules.match_description:
        parts.append(str(getattr(candidate, "description", "") or ""))
    return normalize_match_text(" ".join(parts))


def _rejection(
    code: str,
    message_key: str,
    *,
    default: str,
    **params,
) -> DownloadSelectionRejectionResource:
    return DownloadSelectionRejectionResource(
        code=code,
        message=translate(message_key, default=default, **params),
        group_label=params.get("group_label"),
        term=params.get("term"),
        field=params.get("field"),
        limit=params.get("limit"),
        actual=params.get("actual"),
    )


@dataclass
class CandidateEvaluation:
    candidate: Any
    eligible: bool
    rejection_reasons: list[DownloadSelectionRejectionResource] = field(
        default_factory=list
    )
    matched_required_group_labels: list[str] = field(default_factory=list)
    matched_preferred_group_label: str | None = None
    preference_rank: int | None = None
    selection_rank: int | None = None


@dataclass
class SelectionEvaluationResult:
    ranked_eligible: list[Any]
    preview_order: list[Any]
    input_order: list[Any]
    evaluations: dict[int, CandidateEvaluation]
    eligible_count: int
    rejection_counts: dict[str, int]


def _group_display_name(group: DownloadSelectionKeywordGroup, index: int) -> str:
    return (group.label or "").strip() or str(index + 1)


def evaluate_candidate(
    candidate: Any,
    rules: DownloadSelectionRules,
    *,
    now: datetime,
) -> CandidateEvaluation:
    reasons: list[DownloadSelectionRejectionResource] = []
    indexer_kind = str(getattr(candidate, "indexer_kind", "") or "")
    indexer_name = str(getattr(candidate, "indexer_name", "") or "")
    if rules.indexer_kinds and indexer_kind not in {
        kind.value for kind in rules.indexer_kinds
    }:
        reasons.append(
            _rejection(
                "indexer_kind_not_allowed",
                "download_selection.rejection.indexer_kind_not_allowed",
                default="Indexer kind is not allowed",
                actual=indexer_kind,
            )
        )
    allowed_names = {name.casefold() for name in rules.indexer_names}
    if allowed_names and indexer_name.casefold() not in allowed_names:
        reasons.append(
            _rejection(
                "indexer_not_allowed",
                "download_selection.rejection.indexer_not_allowed",
                default="Indexer is not allowed",
                actual=indexer_name,
            )
        )

    size_bytes = int(getattr(candidate, "size_bytes", 0) or 0)
    if size_bytes < rules.min_size_bytes:
        reasons.append(
            _rejection(
                "size_below_min",
                "download_selection.rejection.size_below_min",
                default="File size is below the minimum",
                field="size_bytes",
                limit=rules.min_size_bytes,
                actual=size_bytes,
            )
        )
    if rules.max_size_bytes is not None and size_bytes > rules.max_size_bytes:
        reasons.append(
            _rejection(
                "size_above_max",
                "download_selection.rejection.size_above_max",
                default="File size exceeds the maximum",
                field="size_bytes",
                limit=rules.max_size_bytes,
                actual=size_bytes,
            )
        )

    seeders = int(getattr(candidate, "seeders", 0) or 0)
    if seeders < rules.min_seeders:
        reasons.append(
            _rejection(
                "seeders_below_min",
                "download_selection.rejection.seeders_below_min",
                default="Seeder count is below the minimum",
                field="seeders",
                limit=rules.min_seeders,
                actual=seeders,
            )
        )

    published_at = getattr(candidate, "published_at", None)
    if rules.max_age_days is not None:
        if published_at is None:
            reasons.append(
                _rejection(
                    "published_at_missing",
                    "download_selection.rejection.published_at_missing",
                    default="Publish date is unavailable",
                    field="published_at",
                )
            )
        else:
            normalized_published_at = (
                published_at
                if published_at.tzinfo is None
                else to_db_utc_naive(published_at)
            )
            if normalized_published_at < now - timedelta(days=rules.max_age_days):
                reasons.append(
                    _rejection(
                        "published_at_too_old",
                        "download_selection.rejection.published_at_too_old",
                        default="Publish date is older than allowed",
                        field="published_at",
                        limit=rules.max_age_days,
                    )
                )

    text = _candidate_text(candidate, rules)
    matched_required: list[str] = []
    for index, group in enumerate(rules.required_groups):
        label = _group_display_name(group, index)
        if _group_matches(text, group):
            matched_required.append(label)
            continue
        reasons.append(
            _rejection(
                "required_group_missing",
                "download_selection.rejection.required_group_missing",
                default="Required keyword group was not matched",
                group_label=label,
            )
        )

    for term in rules.excluded_terms:
        if _term_matches(text, normalize_match_text(term)):
            reasons.append(
                _rejection(
                    "excluded_term",
                    "download_selection.rejection.excluded_term",
                    default="Excluded keyword was matched",
                    term=term,
                )
            )

    matched_preferred_label = None
    preference_rank = None
    for index, group in enumerate(rules.preferred_groups):
        if _group_matches(text, group):
            matched_preferred_label = _group_display_name(group, index)
            preference_rank = index
            break

    return CandidateEvaluation(
        candidate=candidate,
        eligible=not reasons,
        rejection_reasons=reasons,
        matched_required_group_labels=matched_required,
        matched_preferred_group_label=matched_preferred_label,
        preference_rank=preference_rank,
    )


def _published_at_sort_value(value: datetime | None) -> tuple[int, float]:
    if value is None:
        return (1, 0.0)
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return (0, -normalized.timestamp())


def _candidate_sort_key(
    evaluation: CandidateEvaluation,
    rules: DownloadSelectionRules,
) -> tuple:
    candidate = evaluation.candidate
    key: list[Any] = [
        evaluation.preference_rank
        if evaluation.preference_rank is not None
        else len(rules.preferred_groups)
    ]
    priority_index = {
        name.casefold(): index
        for index, name in enumerate(rules.indexer_priority_names)
    }
    for criterion in rules.ranking:
        if criterion == DownloadSelectionRanking.SIZE_DESC:
            key.append(-int(getattr(candidate, "size_bytes", 0) or 0))
        elif criterion == DownloadSelectionRanking.SEEDERS_DESC:
            key.append(-int(getattr(candidate, "seeders", 0) or 0))
        elif criterion == DownloadSelectionRanking.PUBLISHED_AT_DESC:
            key.append(_published_at_sort_value(getattr(candidate, "published_at", None)))
        elif criterion == DownloadSelectionRanking.INDEXER_PRIORITY:
            indexer_name = str(getattr(candidate, "indexer_name", "") or "")
            key.append(priority_index.get(indexer_name.casefold(), len(priority_index)))
    key.extend(
        [
            str(getattr(candidate, "indexer_name", "") or "").casefold(),
            str(getattr(candidate, "title", "") or "").casefold(),
            str(getattr(candidate, "source_uri", "") or ""),
        ]
    )
    return tuple(key)


def evaluate_and_rank(
    candidates: list[Any],
    rules: DownloadSelectionRules,
    *,
    now: datetime,
) -> SelectionEvaluationResult:
    evaluations = {
        id(candidate): evaluate_candidate(candidate, rules, now=now)
        for candidate in candidates
    }
    eligible = [
        evaluation
        for evaluation in evaluations.values()
        if evaluation.eligible
    ]
    eligible.sort(key=lambda item: _candidate_sort_key(item, rules))
    for rank, evaluation in enumerate(eligible, start=1):
        evaluation.selection_rank = rank

    ineligible = [
        evaluation
        for candidate in candidates
        if not (evaluations[id(candidate)].eligible)
        for evaluation in (evaluations[id(candidate)],)
    ]
    ranked_candidates = [evaluation.candidate for evaluation in eligible]
    preview_order = ranked_candidates + [
        evaluation.candidate for evaluation in ineligible
    ]
    rejection_counts = Counter(
        reason.code
        for evaluation in evaluations.values()
        for reason in evaluation.rejection_reasons
    )
    return SelectionEvaluationResult(
        ranked_eligible=ranked_candidates,
        preview_order=preview_order,
        input_order=list(candidates),
        evaluations=evaluations,
        eligible_count=len(eligible),
        rejection_counts=dict(rejection_counts),
    )
