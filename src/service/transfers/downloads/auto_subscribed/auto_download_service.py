"""Automatic download selection for subscribed movies."""

from __future__ import annotations

from typing import Any

from loguru import logger
from peewee import fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import media_exists_expression
from src.model import DownloadTask, Movie
from src.schema.transfers.downloads import DownloadRequestCreateRequest
from src.service.catalog.movie_subscription_search_state_service import (
    ERROR_CODE_NO_CANDIDATE,
    MovieSubscriptionSearchStateService,
    SubscriptionSearchError,
)
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.search_service import DownloadSearchService

MIN_SIZE_BYTES = 1 * 1024 * 1024 * 1024
MAX_SIZE_BYTES = 40 * 1024 * 1024 * 1024
MAX_REJECTED_CANDIDATES = 5
TASK_KEY = "subscribed_movie_auto_download"


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
            "skipped_movies": 0,
            "submitted_movie_numbers": [],
            "no_candidate_movie_numbers": [],
            "failed_items": [],
        }

    def _process_one(self, shared: dict[str, Any], movie: Movie) -> None:
        movie_number = movie.movie_number
        shared["searched_movies"] += 1
        try:
            candidates = self.download_search_service.search_candidates(movie_number=movie_number)
        except Exception as exc:
            shared["failed_items"].append(
                {"movie_number": movie_number, "stage": "search", "detail": str(exc)}
            )
            raise SubscriptionSearchError("indexer_search_failed", str(exc), consumes_budget=False) from exc

        candidates = [candidate for candidate in candidates if self._candidate_usable(candidate)]
        candidates.sort(key=lambda candidate: (-candidate.size_bytes, candidate.indexer_name, candidate.title))
        response = None
        for candidate in candidates[:MAX_REJECTED_CANDIDATES]:
            try:
                response = self.download_request_service.create_request(
                    DownloadRequestCreateRequest(
                        client_id=candidate.resolved_client_id,
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
                break
            except ApiError as exc:
                if exc.status_code in {404, 422, 503}:
                    logger.info(
                        "Auto download candidate rejected movie_number={} title={} code={}",
                        movie_number,
                        candidate.title,
                        exc.code,
                    )
                    continue
                shared["failed_items"].append(
                    {"movie_number": movie_number, "stage": "submit", "detail": str(exc)}
                )
                raise SubscriptionSearchError("download_submit_failed", str(exc), consumes_budget=False) from exc
        if response is None:
            shared["no_candidate_movies"] += 1
            shared["no_candidate_movie_numbers"].append(movie_number)
            raise SubscriptionSearchError(
                ERROR_CODE_NO_CANDIDATE,
                "未找到可用资源",
                consumes_budget=True,
            )
        if response.created:
            shared["submitted_movies"] += 1
            shared["submitted_movie_numbers"].append(movie_number)
        else:
            shared["skipped_movies"] += 1

    @staticmethod
    def _candidate_usable(candidate) -> bool:
        return bool(
            (candidate.source_uri or "").strip()
            and MIN_SIZE_BYTES <= candidate.size_bytes <= MAX_SIZE_BYTES
            and candidate.seeders > 0
        )

    def run(self, *, reporter) -> dict[str, Any]:
        candidate_ids = self._select_candidate_ids()
        shared = self._setup_run()
        total = len(candidate_ids)
        for current, movie_id in enumerate(candidate_ids, start=1):
            movie = MovieSubscriptionSearchStateService.begin_attempt(movie_id)
            if movie is None:
                shared["skipped_movies"] += 1
                reporter.emit(current=current, total=total)
                continue
            try:
                self._process_one(shared, movie)
                MovieSubscriptionSearchStateService.mark_succeeded(movie.id)
            except SubscriptionSearchError as exc:
                MovieSubscriptionSearchStateService.mark_failed(movie, exc)
            except Exception as exc:
                shared["failed_items"].append(
                    {"movie_number": movie.movie_number, "stage": "process", "detail": str(exc)}
                )
                MovieSubscriptionSearchStateService.mark_failed(
                    movie,
                    SubscriptionSearchError(
                        "subscription_search_process_failed", str(exc), consumes_budget=False
                    ),
                )
            reporter.emit(current=current, total=total)
        return {
            "candidate_movies": len(candidate_ids),
            "searched_movies": shared["searched_movies"],
            "submitted_movies": shared["submitted_movies"],
            "no_candidate_movies": shared["no_candidate_movies"],
            "skipped_movies": shared["skipped_movies"],
            "failed_movies": len(shared["failed_items"]),
            "submitted_movie_numbers": shared["submitted_movie_numbers"],
            "no_candidate_movie_numbers": shared["no_candidate_movie_numbers"],
            "failed_items": shared["failed_items"],
        }

    @staticmethod
    def _select_candidate_ids() -> list[int]:
        from src.common.runtime_time import utc_now_for_db

        query = (
            Movie.select(Movie.id)
            .where(Movie.is_subscribed == True)
            .where(~media_exists_expression())
            .where(
                ~fn.EXISTS(
                    DownloadTask.select(DownloadTask.id).where(
                        (DownloadTask.movie == Movie.movie_number)
                        & DownloadTask.state.in_(
                            ("queued", "downloading", "completed")
                        )
                    )
                )
            )
            .where(MovieSubscriptionSearchStateService.candidate_condition(now=utc_now_for_db()))
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )
        return [int(movie_id) for movie_id, in query.tuples()]
