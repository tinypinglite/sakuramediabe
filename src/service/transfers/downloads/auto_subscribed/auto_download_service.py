"""Automatic download selection for subscribed movies."""

from __future__ import annotations

from typing import Any

from loguru import logger
from peewee import fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import media_exists_expression
from src.i18n import translate
from src.model import BackgroundTaskRun, DownloadTask, Media, Movie
from src.schema.transfers.downloads import DownloadRequestCreateRequest
from src.service.catalog.movie_subscription_search_state_service import (
    ERROR_CODE_FILTERED_BY_POLICY,
    ERROR_CODE_NO_CANDIDATE,
    MovieSubscriptionSearchStateService,
    SubscriptionSearchError,
)
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.search_service import DownloadSearchService
from src.service.transfers.downloads.selection import defaults as selection_defaults
from src.service.transfers.downloads.selection.evaluator import evaluate_and_rank
from src.service.transfers.downloads.selection.policy_service import (
    DownloadSelectionPolicyService,
)
from src.service.transfers.shared.common import (
    unfinished_import_download_task_exists_expression,
)

TASK_KEY = "subscribed_movie_auto_download"
CLIENT_ROLLOVER_STATUS_CODES = frozenset({401, 404, 422, 503})
BLACKLISTED_CANDIDATE_CODE = "download_source_blacklisted"
MIN_SIZE_BYTES = selection_defaults.MIN_SIZE_BYTES
MAX_SIZE_BYTES = selection_defaults.MAX_SIZE_BYTES
MAX_REJECTED_CANDIDATES = selection_defaults.MAX_REJECTED_CANDIDATES


class SubscribedMovieAutoDownloadService:
    def __init__(
        self,
        *,
        download_search_service: DownloadSearchService | None = None,
        download_request_service: DownloadRequestService | None = None,
    ):
        self.download_search_service = download_search_service or DownloadSearchService()
        self.download_request_service = download_request_service or DownloadRequestService()

    def _setup_run(self) -> dict[str, Any]:
        return {
            "searched_movies": 0,
            "submitted_movies": 0,
            "no_candidate_movies": 0,
            "policy_filtered_movies": 0,
            "skipped_movies": 0,
            "raw_candidate_count": 0,
            "system_candidate_count": 0,
            "policy_eligible_candidate_count": 0,
            "policy_rejection_counts": {},
            "submitted_movie_numbers": [],
            "no_candidate_movie_numbers": [],
            "policy_filtered_movie_numbers": [],
            "failed_items": [],
        }

    @staticmethod
    def _library_import_running() -> bool:
        from src.service.transfers.shared.import_task_service import ImportTaskService

        return (
            BackgroundTaskRun.select(BackgroundTaskRun.id)
            .where(
                (BackgroundTaskRun.task_key == ImportTaskService.TASK_KEY)
                & (BackgroundTaskRun.state == "running")
            )
            .exists()
        )

    @staticmethod
    def _media_exists(movie_number: str) -> bool:
        return Media.select(Media.id).where(Media.movie == movie_number).exists()

    def _process_one(
        self,
        shared: dict[str, Any],
        movie: Movie,
        *,
        selection_rules,
    ) -> None:
        movie_number = movie.movie_number
        shared["searched_movies"] += 1
        try:
            search_result = self.download_search_service.search_candidates_with_stats(
                movie_number=movie_number
            )
        except Exception as exc:
            shared["failed_items"].append(
                {"movie_number": movie_number, "stage": "search", "detail": str(exc)}
            )
            raise SubscriptionSearchError("indexer_search_failed", str(exc), consumes_budget=False) from exc

        shared["raw_candidate_count"] += search_result.raw_candidate_count
        shared["system_candidate_count"] += search_result.system_candidate_count
        if not search_result.candidates:
            shared["no_candidate_movies"] += 1
            shared["no_candidate_movie_numbers"].append(movie_number)
            raise SubscriptionSearchError(
                ERROR_CODE_NO_CANDIDATE,
                translate(
                    "download_selection.status.no_candidate",
                    default="No downloadable resource found",
                ),
                consumes_budget=True,
            )

        from src.common.runtime_time import utc_now_for_db

        selection_result = evaluate_and_rank(
            search_result.candidates,
            selection_rules,
            now=utc_now_for_db(),
        )
        shared["policy_eligible_candidate_count"] += selection_result.eligible_count
        for code, count in selection_result.rejection_counts.items():
            shared["policy_rejection_counts"][code] = (
                shared["policy_rejection_counts"].get(code, 0) + count
            )
        if not selection_result.ranked_eligible:
            shared["policy_filtered_movies"] += 1
            shared["policy_filtered_movie_numbers"].append(movie_number)
            raise SubscriptionSearchError(
                ERROR_CODE_FILTERED_BY_POLICY,
                translate(
                    "download_selection.status.filtered_by_policy",
                    default="Download candidates exist but none match the selection policy",
                ),
                consumes_budget=False,
            )

        if self._media_exists(movie_number):
            # 候选是任务开始时一次性选出的，导入可能在搜索期间已写完 media。
            shared["skipped_movies"] += 1
            logger.info("Auto download skipped existing media movie_number={}", movie_number)
            return
        response = None
        for candidate in selection_result.ranked_eligible[:MAX_REJECTED_CANDIDATES]:
            for client_id in self._candidate_client_ids(candidate):
                try:
                    response = self.download_request_service.create_request(
                        DownloadRequestCreateRequest(
                            client_id=client_id,
                            movie_number=movie_number,
                            candidate={
                                "source_uri": candidate.source_uri,
                                "indexer_name": candidate.indexer_name,
                                "title": candidate.title,
                                "size_bytes": candidate.size_bytes,
                                "seeders": candidate.seeders,
                            },
                        )
                    )
                except ApiError as exc:
                    if exc.code == BLACKLISTED_CANDIDATE_CODE:
                        logger.info(
                            "Auto download candidate rejected movie_number={} title={} code={}",
                            movie_number,
                            candidate.title,
                            exc.code,
                        )
                        break
                    if exc.status_code in CLIENT_ROLLOVER_STATUS_CODES:
                        logger.info(
                            "Auto download client rejected movie_number={} client_id={} "
                            "title={} code={}",
                            movie_number,
                            client_id,
                            candidate.title,
                            exc.code,
                        )
                        continue
                    shared["failed_items"].append(
                        {"movie_number": movie_number, "stage": "submit", "detail": str(exc)}
                    )
                    raise SubscriptionSearchError(
                        "download_submit_failed", str(exc), consumes_budget=False
                    ) from exc
                if movie.media_replacement_pending and not response.created:
                    # 补库时复用已有远端任务不会重新下载、也不会重跑导入，本轮拿不到媒体；
                    # 该候选在本客户端不可用，换下一个候选。
                    logger.info(
                        "Auto download replacement candidate already exists "
                        "movie_number={} client_id={} title={}",
                        movie_number,
                        client_id,
                        candidate.title,
                    )
                    response = None
                    break
                break
            if response is not None:
                break
        if response is None:
            shared["no_candidate_movies"] += 1
            shared["no_candidate_movie_numbers"].append(movie_number)
            raise SubscriptionSearchError(
                ERROR_CODE_NO_CANDIDATE,
                translate(
                    "download_selection.status.no_candidate",
                    default="No downloadable resource found",
                ),
                consumes_budget=True,
            )
        if response.created:
            shared["submitted_movies"] += 1
            shared["submitted_movie_numbers"].append(movie_number)
        else:
            shared["skipped_movies"] += 1

    @staticmethod
    def _candidate_client_ids(candidate) -> list[int]:
        """按绑定顺序尝试候选的全部下载器，默认下载器始终排第一。"""
        client_ids = [int(candidate.resolved_client_id)]
        for client in getattr(candidate, "download_clients", ()) or ():
            client_id = int(client.id)
            if client_id not in client_ids:
                client_ids.append(client_id)
        return client_ids

    def run(self, *, reporter) -> dict[str, Any]:
        if self._library_import_running():
            logger.info("Auto download skipped reason=library_import_running")
            summary = self._setup_run()
            summary.update(
                {
                    "candidate_movies": 0,
                    "failed_movies": 0,
                    "skipped": True,
                    "reason": "library_import_running",
                }
            )
            return summary

        selection_policy = DownloadSelectionPolicyService.get_resource()
        candidate_ids = self._select_candidate_ids()
        shared = self._setup_run()
        shared["selection_policy_version"] = selection_policy.version
        total = len(candidate_ids)
        logger.info("Auto download started candidate_movies={}", total)
        step = max(total // 20, 1)

        def summary_patch() -> dict[str, Any]:
            return {
                "searched_movies": shared["searched_movies"],
                "submitted_movies": shared["submitted_movies"],
                "no_candidate_movies": shared["no_candidate_movies"],
                "policy_filtered_movies": shared["policy_filtered_movies"],
                "skipped_movies": shared["skipped_movies"],
                "failed_movies": len(shared["failed_items"]),
                "raw_candidate_count": shared["raw_candidate_count"],
                "system_candidate_count": shared["system_candidate_count"],
                "policy_eligible_candidate_count": shared[
                    "policy_eligible_candidate_count"
                ],
                "policy_rejection_counts": dict(shared["policy_rejection_counts"]),
                "selection_policy_version": shared["selection_policy_version"],
            }

        def progress_text(completed: int, *, action: str | None = None) -> str:
            fragments = [
                translate("tasks.subscribed_movie_auto_download.progress.title")
            ]
            if action:
                fragments.append(action)
            fragments.append(
                translate(
                    "tasks.subscribed_movie_auto_download.progress.stats",
                    current=completed,
                    total=total,
                    submitted=shared["submitted_movies"],
                    no_candidate=shared["no_candidate_movies"],
                    policy_filtered=shared["policy_filtered_movies"],
                    failed=len(shared["failed_items"]),
                )
            )
            return " · ".join(fragments)

        for current, movie_id in enumerate(candidate_ids, start=1):
            movie = MovieSubscriptionSearchStateService.begin_attempt(movie_id)
            if movie is None:
                shared["skipped_movies"] += 1
                reporter.emit(
                    current=current,
                    total=total,
                    text=progress_text(current),
                    summary_patch=summary_patch(),
                )
                continue
            reporter.emit(
                current=current - 1,
                total=total,
                text=progress_text(
                    current - 1,
                    action=translate(
                        "tasks.subscribed_movie_auto_download.progress.searching",
                        movie_number=movie.movie_number,
                    ),
                ),
                summary_patch=summary_patch(),
            )
            try:
                self._process_one(
                    shared,
                    movie,
                    selection_rules=selection_policy.rules,
                )
                MovieSubscriptionSearchStateService.mark_succeeded(movie.id)
            except SubscriptionSearchError as exc:
                MovieSubscriptionSearchStateService.mark_failed(movie, exc)
            except Exception as exc:
                logger.warning(
                    "Auto download failed movie_number={} detail={}",
                    movie.movie_number,
                    exc,
                )
                shared["failed_items"].append(
                    {"movie_number": movie.movie_number, "stage": "process", "detail": str(exc)}
                )
                MovieSubscriptionSearchStateService.mark_failed(
                    movie,
                    SubscriptionSearchError(
                        "subscription_search_process_failed", str(exc), consumes_budget=False
                    ),
                )
            reporter.emit(
                current=current,
                total=total,
                text=progress_text(current),
                summary_patch=summary_patch(),
            )
            if current == 1 or current % step == 0:
                logger.info(
                    "Auto download progress completed={}/{} submitted={} no_candidate={} skipped={} failed={}",
                    current,
                    total,
                    shared["submitted_movies"],
                    shared["no_candidate_movies"],
                    shared["skipped_movies"],
                    len(shared["failed_items"]),
                )
        return {
            "candidate_movies": len(candidate_ids),
            "searched_movies": shared["searched_movies"],
            "submitted_movies": shared["submitted_movies"],
            "no_candidate_movies": shared["no_candidate_movies"],
            "policy_filtered_movies": shared["policy_filtered_movies"],
            "skipped_movies": shared["skipped_movies"],
            "failed_movies": len(shared["failed_items"]),
            "raw_candidate_count": shared["raw_candidate_count"],
            "system_candidate_count": shared["system_candidate_count"],
            "policy_eligible_candidate_count": shared[
                "policy_eligible_candidate_count"
            ],
            "policy_rejection_counts": dict(shared["policy_rejection_counts"]),
            "submitted_movie_numbers": shared["submitted_movie_numbers"],
            "no_candidate_movie_numbers": shared["no_candidate_movie_numbers"],
            "policy_filtered_movie_numbers": shared[
                "policy_filtered_movie_numbers"
            ],
            "failed_items": shared["failed_items"],
            "selection_policy_version": shared["selection_policy_version"],
        }

    @staticmethod
    def _select_candidate_ids() -> list[int]:
        from src.common.runtime_time import utc_now_for_db

        movie_match = DownloadTask.movie == Movie.movie_number
        ongoing_tasks = DownloadTask.select(DownloadTask.id).where(
            movie_match & DownloadTask.state.in_(("queued", "downloading"))
        )
        completed_tasks = DownloadTask.select(DownloadTask.id).where(
            movie_match & (DownloadTask.state == "completed")
        )
        query = (
            Movie.select(Movie.id)
            .where(Movie.is_subscribed == True)
            .where(~media_exists_expression())
            .where(~fn.EXISTS(ongoing_tasks))
            # 与订阅状态展示共用同一口径：只在途任务（含下完等导入）算"进行中"，
            # 下载失败的终态行不挡下一轮搜索。
            .where(~unfinished_import_download_task_exists_expression())
            .where(
                (Movie.media_replacement_pending == True)
                | ~fn.EXISTS(completed_tasks)
            )
            .where(MovieSubscriptionSearchStateService.candidate_condition(now=utc_now_for_db()))
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )
        return [int(movie_id) for movie_id, in query.tuples()]
