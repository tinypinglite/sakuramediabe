from __future__ import annotations

from typing import Any, Dict, List, Sequence

from loguru import logger
from peewee import fn

from src.common.service_helpers import media_exists_expression
from src.config.config import IndexerKind, settings
from src.model import DownloadTask, Media, Movie
from src.model.enums import DownloadClientKind
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

    _media_exists_expression = staticmethod(media_exists_expression)

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
            .where(~self._media_exists_expression())
            .where(~self._download_task_exists_expression())
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )
        return list(query)

    @staticmethod
    def _is_cloud115_preferred() -> bool:
        """全局下载器偏好首项是否为 115：决定本轮选种走「网盘优先」策略。"""
        preferred_kinds = settings.downloads.preferred_client_kinds or []
        return bool(preferred_kinds) and preferred_kinds[0] == DownloadClientKind.CLOUD115.value

    def _pick_best_candidate(
        self,
        candidates: Sequence[DownloadCandidateResource],
    ) -> DownloadCandidateResource | None:
        # 策略在整轮选种内取一次，避免同一批候选被两套规则切开。
        cloud115_preferred = self._is_cloud115_preferred()
        filtered_candidates = [
            candidate
            for candidate in candidates
            if self._is_usable_candidate(candidate, cloud115_preferred=cloud115_preferred)
        ]
        if not filtered_candidates:
            return None

        candidate_pool = filtered_candidates
        if cloud115_preferred:
            # 网盘优先：pt 资源只能落到本地 qb（PT 索引器禁绑 115），违背「内容进 115」的意图，
            # 降为兜底层，只有 bt 池整体为空时才回落到含 pt 的全池。该分层在 4K 分层之外，
            # 即「pt 有 4K、bt 只有普通版」时仍选 bt。
            bt_candidates = [
                candidate
                for candidate in candidate_pool
                if candidate.indexer_kind != IndexerKind.PT.value
            ]
            candidate_pool = bt_candidates or candidate_pool

        four_k_candidates = [
            candidate for candidate in candidate_pool if BLURAY_TAG in (candidate.tags or [])
        ]
        candidate_pool = four_k_candidates or candidate_pool
        return sorted(candidate_pool, key=self._candidate_sort_key)[0]

    @staticmethod
    def _is_usable_candidate(
        candidate: DownloadCandidateResource,
        *,
        cloud115_preferred: bool,
    ) -> bool:
        has_source = bool((candidate.magnet_url or "").strip() or (candidate.torrent_url or "").strip())
        if not has_source:
            return False
        # 115 离线是云端拉取，本地做种数无参考意义，且部分索引器根本不返回 seeders（恒 0）；
        # 网盘优先时对 bt 候选取消该门槛。pt 候选必然落到 qb，门槛照旧生效。
        skip_seeders_check = cloud115_preferred and candidate.indexer_kind == IndexerKind.BT.value
        if not skip_seeders_check and candidate.seeders < MIN_SEEDERS:
            return False
        return MIN_SIZE_BYTES <= candidate.size_bytes <= MAX_SIZE_BYTES

    @staticmethod
    def _candidate_sort_key(candidate: DownloadCandidateResource) -> tuple:
        tags = candidate.tags or []
        # 第一维「pt 优先」只在 qb 优先策略下生效：115 优先且 bt 池非空时池内已无 pt，
        # 回落到全池时池内又全是 pt，两种情况该维度都无差别，因此无需按策略反转。
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
