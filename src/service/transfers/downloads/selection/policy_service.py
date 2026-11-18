"""全局选种策略的持久化、预览和候选评估入口。"""

from __future__ import annotations

from peewee import IntegrityError
from pydantic import ValidationError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadSelectionPolicy, Indexer
from src.schema.transfers.download_selection import (
    DownloadSelectionPolicyResource,
    DownloadSelectionPolicyUpdateRequest,
    DownloadSelectionPreviewRequest,
    DownloadSelectionPreviewResponse,
    DownloadSelectionRules,
)
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadCandidateSelectionEvaluationResource,
)
from src.service.transfers.downloads.search_service import DownloadSearchService
from src.service.transfers.downloads.selection.defaults import (
    MAX_REJECTED_CANDIDATES,
    legacy_selection_rules,
)
from src.service.transfers.downloads.selection.evaluator import evaluate_and_rank


class DownloadSelectionPolicyService:
    @staticmethod
    def _get_or_create_row(*, for_update: bool = False):
        query = DownloadSelectionPolicy.select().where(DownloadSelectionPolicy.id == 1)
        if for_update:
            query = query.for_update()
        row = query.get_or_none()
        if row is not None:
            return row
        try:
            return DownloadSelectionPolicy.create(
                id=1,
                rules=legacy_selection_rules().model_dump(mode="json"),
                version=1,
            )
        except IntegrityError:
            row = (
                DownloadSelectionPolicy.select()
                .where(DownloadSelectionPolicy.id == 1)
                .get_or_none()
            )
            if row is None:
                raise
            return row

    @classmethod
    def _resource(cls, row) -> DownloadSelectionPolicyResource:
        try:
            return DownloadSelectionPolicyResource.from_model(row)
        except ValidationError as exc:
            raise ApiError(
                500,
                "download_selection_policy_invalid",
                "Download selection policy is invalid",
                {"errors": exc.errors(include_url=False, include_context=False)},
            ) from exc

    @classmethod
    def get_resource(cls) -> DownloadSelectionPolicyResource:
        return cls._resource(cls._get_or_create_row())

    @classmethod
    def get_rules(cls) -> DownloadSelectionRules:
        return cls.get_resource().rules

    @staticmethod
    def _validate_indexer_references(rules: DownloadSelectionRules) -> None:
        known_names = {
            row[0].casefold()
            for row in Indexer.select(Indexer.name).tuples()
            if row[0]
        }
        referenced = {
            name.casefold()
            for name in [*rules.indexer_names, *rules.indexer_priority_names]
        }
        missing = sorted(referenced - known_names)
        if missing:
            raise ApiError(
                422,
                "invalid_download_selection_indexers",
                "Download selection policy references unknown indexers",
                {"indexers": missing},
            )

    @classmethod
    def update(cls, payload: DownloadSelectionPolicyUpdateRequest):
        cls._validate_indexer_references(payload.rules)
        database = DownloadSelectionPolicy._meta.database
        with database.atomic():
            row = cls._get_or_create_row(for_update=True)
            if row.version != payload.expected_version:
                raise ApiError(
                    409,
                    "download_selection_policy_version_conflict",
                    "Download selection policy has changed",
                    {
                        "expected_version": payload.expected_version,
                        "actual_version": row.version,
                    },
                )
            row.rules = payload.rules.model_dump(mode="json")
            row.version += 1
            row.save(only=[DownloadSelectionPolicy.rules, DownloadSelectionPolicy.version])
        return cls._resource(row)

    @staticmethod
    def _evaluated_resources(
        candidates: list[DownloadCandidateResource],
        rules: DownloadSelectionRules,
    ):
        result = evaluate_and_rank(
            list(candidates),
            rules,
            now=utc_now_for_db(),
        )

        def annotated(candidate: DownloadCandidateResource):
            evaluation = result.evaluations[id(candidate)]
            return candidate.model_copy(
                update={
                    "selection_evaluation": DownloadCandidateSelectionEvaluationResource(
                        eligible=evaluation.eligible,
                        rejection_reasons=evaluation.rejection_reasons,
                        matched_required_group_labels=(
                            evaluation.matched_required_group_labels
                        ),
                        matched_preferred_group_label=(
                            evaluation.matched_preferred_group_label
                        ),
                        preference_rank=evaluation.preference_rank,
                        selection_rank=evaluation.selection_rank,
                        within_attempt_limit=(
                            evaluation.selection_rank is not None
                            and evaluation.selection_rank
                            <= MAX_REJECTED_CANDIDATES
                        ),
                    )
                }
            )

        return result, {
            id(candidate): annotated(candidate) for candidate in candidates
        }

    @classmethod
    def evaluate_for_manual(
        cls,
        candidates: list[DownloadCandidateResource],
    ) -> list[DownloadCandidateResource]:
        if not candidates:
            return []
        resource = cls.get_resource()
        _result, annotated = cls._evaluated_resources(candidates, resource.rules)
        return [annotated[id(candidate)] for candidate in candidates]

    @classmethod
    def preview(
        cls,
        payload: DownloadSelectionPreviewRequest,
    ) -> DownloadSelectionPreviewResponse:
        if payload.rules is not None:
            cls._validate_indexer_references(payload.rules)
            rules = payload.rules
            policy_version = None
            draft = True
        else:
            policy = cls.get_resource()
            rules = policy.rules
            policy_version = policy.version
            draft = False

        search_result = DownloadSearchService().search_candidates_with_stats(
            movie_number=payload.movie_number,
        )
        result, annotated = cls._evaluated_resources(
            search_result.candidates,
            rules,
        )
        ordered_candidates = [
            annotated[id(candidate)] for candidate in result.preview_order
        ]
        selected_source_uri = (
            ordered_candidates[0].source_uri
            if result.ranked_eligible
            else None
        )
        return DownloadSelectionPreviewResponse(
            draft=draft,
            policy_version=policy_version,
            raw_candidate_count=search_result.raw_candidate_count,
            system_candidate_count=search_result.system_candidate_count,
            eligible_candidate_count=result.eligible_count,
            policy_filtered_count=(
                search_result.system_candidate_count - result.eligible_count
            ),
            rejection_counts=result.rejection_counts,
            selected_source_uri=selected_source_uri,
            candidates=ordered_candidates,
        )
