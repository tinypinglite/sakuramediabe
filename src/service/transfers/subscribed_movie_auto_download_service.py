from __future__ import annotations

from typing import Any, Dict, List, Sequence

from loguru import logger
from peewee import fn

from src.common.service_helpers import playable_exists_expression
from src.model import DownloadTask, Media, Movie
from src.schema.transfers.downloads import (
    DownloadCandidateCreatePayload,
    DownloadCandidateResource,
    DownloadRequestCreateRequest,
)
from src.service.transfers.download_request_service import DownloadRequestService
from src.service.transfers.download_search_service import DownloadSearchService
from src.service.transfers.tag_rules import BLURAY_TAG, SUBTITLE_TAG

MIN_SEEDERS = 3
MIN_SIZE_BYTES = 1 * 1024 * 1024 * 1024
MAX_SIZE_BYTES = 40 * 1024 * 1024 * 1024


class SubscribedMovieAutoDownloadService:
    def __init__(
        self,
        *,
        download_search_service: DownloadSearchService | None = None,
        download_request_service: DownloadRequestService | None = None,
    ):
        self.download_search_service = download_search_service or DownloadSearchService()
        self.download_request_service = download_request_service or DownloadRequestService()

    def run(self) -> Dict[str, Any]:
        movies = self._list_candidate_movies()
        summary: Dict[str, Any] = {
            "candidate_movies": len(movies),
            "searched_movies": 0,
            "submitted_movies": 0,
            "no_candidate_movies": 0,
            "skipped_movies": 0,
            "failed_movies": 0,
            "submitted_movie_numbers": [],
            "no_candidate_movie_numbers": [],
            "failed_items": [],
        }

        for movie in movies:
            movie_number = movie.movie_number
            summary["searched_movies"] += 1
            logger.info("Auto download searching candidates for movie_number={}", movie_number)
            try:
                candidates = self.download_search_service.search_candidates(movie_number=movie_number)
            except Exception as exc:
                self._record_failure(summary, movie_number, stage="search", detail=str(exc))
                logger.exception(
                    "Auto download candidate search failed movie_number={} detail={}",
                    movie_number,
                    exc,
                )
                continue

            candidate = self._pick_best_candidate(candidates)
            if candidate is None:
                summary["no_candidate_movies"] += 1
                summary["no_candidate_movie_numbers"].append(movie_number)
                logger.info("Auto download found no usable candidate movie_number={}", movie_number)
                continue

            payload = DownloadRequestCreateRequest(
                movie_number=movie_number,
                candidate=self._build_candidate_payload(candidate),
            )
            try:
                response = self.download_request_service.create_request(payload)
            except Exception as exc:
                self._record_failure(summary, movie_number, stage="submit", detail=str(exc))
                logger.exception(
                    "Auto download submit failed movie_number={} title={} detail={}",
                    movie_number,
                    candidate.title,
                    exc,
                )
                continue

            if response.created:
                summary["submitted_movies"] += 1
                summary["submitted_movie_numbers"].append(movie_number)
                logger.info(
                    "Auto download submitted movie_number={} title={} info_hash={}",
                    movie_number,
                    response.task.name,
                    response.task.info_hash,
                )
                continue

            summary["skipped_movies"] += 1
            logger.info(
                "Auto download skipped existing request movie_number={} title={}",
                movie_number,
                candidate.title,
            )

        return summary

    @staticmethod
    def _record_failure(summary: Dict[str, Any], movie_number: str, *, stage: str, detail: str) -> None:
        summary["failed_movies"] += 1
        summary["failed_items"].append(
            {
                "movie_number": movie_number,
                "stage": stage,
                "detail": detail,
            }
        )

    _playable_exists_expression = staticmethod(playable_exists_expression)

    @staticmethod
    def _download_task_exists_expression():
        existing_tasks = DownloadTask.select(DownloadTask.id).where(
            fn.UPPER(fn.TRIM(DownloadTask.movie)) == fn.UPPER(fn.TRIM(Movie.movie_number))
        )
        return fn.EXISTS(existing_tasks)

    def _list_candidate_movies(self) -> List[Movie]:
        query = (
            Movie.select()
            .where(Movie.is_subscribed == True)
            .where(~self._playable_exists_expression())
            .where(~self._download_task_exists_expression())
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )
        return list(query)

    def _pick_best_candidate(
        self,
        candidates: Sequence[DownloadCandidateResource],
    ) -> DownloadCandidateResource | None:
        filtered_candidates = [candidate for candidate in candidates if self._is_usable_candidate(candidate)]
        if not filtered_candidates:
            return None

        four_k_candidates = [
            candidate for candidate in filtered_candidates if BLURAY_TAG in (candidate.tags or [])
        ]
        candidate_pool = four_k_candidates or filtered_candidates
        return sorted(candidate_pool, key=self._candidate_sort_key)[0]

    @staticmethod
    def _is_usable_candidate(candidate: DownloadCandidateResource) -> bool:
        has_source = bool((candidate.magnet_url or "").strip() or (candidate.torrent_url or "").strip())
        if not has_source:
            return False
        if candidate.seeders < MIN_SEEDERS:
            return False
        return MIN_SIZE_BYTES <= candidate.size_bytes <= MAX_SIZE_BYTES

    @staticmethod
    def _candidate_sort_key(candidate: DownloadCandidateResource) -> tuple:
        tags = candidate.tags or []
        return (
            0 if candidate.indexer_kind == "pt" else 1,
            0 if SUBTITLE_TAG in tags else 1,
            -candidate.seeders,
            -candidate.size_bytes,
            candidate.indexer_name,
            candidate.title,
        )

    @staticmethod
    def _build_candidate_payload(candidate: DownloadCandidateResource) -> DownloadCandidateCreatePayload:
        return DownloadCandidateCreatePayload(
            source=candidate.source,
            indexer_name=candidate.indexer_name,
            indexer_kind=candidate.indexer_kind,
            title=candidate.title,
            size_bytes=candidate.size_bytes,
            seeders=candidate.seeders,
            magnet_url=candidate.magnet_url,
            torrent_url=candidate.torrent_url,
            tags=list(candidate.tags or []),
        )
