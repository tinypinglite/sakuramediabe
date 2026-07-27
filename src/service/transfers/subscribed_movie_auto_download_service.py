from __future__ import annotations

from typing import Any, Dict, List, Sequence

from loguru import logger
from peewee import JOIN

from src.common.service_helpers import media_exists_expression
from src.config.config import IndexerKind, settings
from src.model import DownloadTask, Movie, ResourceTaskState
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadCandidateCreatePayload,
    DownloadCandidateResource,
    DownloadRequestCreateRequest,
)
from src.service.transfers.common import (
    active_download_task_exists_expression,
    canonicalize_btih,
    download_task_dead_expression,
)
from src.service.transfers.download_request_service import DownloadRequestService
from src.service.transfers.download_search_service import DownloadSearchService
from src.service.transfers.subscribed_movie_search_state_service import (
    SubscribedMovieSearchStateService,
    search_state_join_condition,
)
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
        due_movies = self._collect_due_movies()
        summary: Dict[str, Any] = {
            "candidate_movies": len(due_movies),
            "searched_movies": 0,
            "submitted_movies": 0,
            "no_candidate_movies": 0,
            "newly_exhausted_movies": 0,
            "skipped_movies": 0,
            "failed_movies": 0,
            "submitted_movie_numbers": [],
            "no_candidate_movie_numbers": [],
            "failed_items": [],
        }

        for movie in due_movies:
            movie_number = movie.movie_number
            summary["searched_movies"] += 1
            logger.info("Auto download searching candidates for movie_number={}", movie_number)
            try:
                candidates = self.download_search_service.search_candidates(movie_number=movie_number)
            except Exception as exc:
                self._record_failure(summary, movie_number, stage="search", detail=str(exc))
                # 索引器故障不消耗该影片的查询预算，下一轮照常重试。
                SubscribedMovieSearchStateService.record_search_error(movie, str(exc))
                logger.exception(
                    "Auto download candidate search failed movie_number={} detail={}",
                    movie_number,
                    exc,
                )
                continue

            # 该影片已经试过并判死的种子：重查时必须排除，否则确定性排序会把同一个死种反复选中。
            excluded_info_hashes = self._list_dead_info_hashes(movie_number)
            candidate = self._pick_best_candidate(candidates, excluded_info_hashes)
            if candidate is None:
                summary["no_candidate_movies"] += 1
                summary["no_candidate_movie_numbers"].append(movie_number)
                state = SubscribedMovieSearchStateService.record_attempt(movie, submitted=False)
                if state.state == SubscribedMovieSearchStateService.STATE_EXHAUSTED:
                    summary["newly_exhausted_movies"] += 1
                logger.info(
                    "Auto download found no usable candidate movie_number={} excluded_dead={} state={}",
                    movie_number,
                    len(excluded_info_hashes),
                    state.state,
                )
                continue

            payload = DownloadRequestCreateRequest(
                movie_number=movie_number,
                candidate=self._build_candidate_payload(candidate),
            )
            try:
                response = self.download_request_service.create_request(payload)
            except Exception as exc:
                self._record_failure(summary, movie_number, stage="submit", detail=str(exc))
                SubscribedMovieSearchStateService.record_search_error(movie, str(exc))
                logger.exception(
                    "Auto download submit failed movie_number={} title={} detail={}",
                    movie_number,
                    candidate.title,
                    exc,
                )
                continue

            SubscribedMovieSearchStateService.record_attempt(movie, submitted=True)
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

    @classmethod
    def _collect_due_movies(cls) -> List[Movie]:
        """本轮该发起搜索的订阅影片，一条 SQL 出结果。

        不需要任何 Python 侧的到期筛选：放弃与否在 record_attempt 写入时就落进了 state，
        所以"还要不要查"退化成一次纯集合判定。没有状态行（LEFT JOIN 出 NULL）= 从未查过 = 要查。
        """
        return list(
            Movie.select(Movie)
            .join(ResourceTaskState, JOIN.LEFT_OUTER, on=search_state_join_condition())
            .where(Movie.is_subscribed == True)
            .where(~cls._media_exists_expression())
            .where(~active_download_task_exists_expression())
            .where(
                ResourceTaskState.state.is_null(True)
                | (ResourceTaskState.state != SubscribedMovieSearchStateService.STATE_EXHAUSTED)
            )
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )

    @staticmethod
    def _list_dead_info_hashes(movie_number: str) -> set[str]:
        """取该影片所有已判死的下载任务 info_hash，作为本轮选种黑名单。

        DownloadTask 本身就是台账，因此黑名单不需要额外的表或字段。黑名单是永久的：info_hash
        内容寻址，同一个 hash 就是同一个 swarm，换索引器它照样是死的。要重试某个具体种子，
        从 qB 里删掉它即可（_prune_ghost_tasks 的反向对账会同步删掉本地行）。
        """
        # 入参是 Movie 行的规范番号，download_task.movie_number 由提交链路拷贝同一列，
        # 两侧直接裸列精确比较；套 UPPER(TRIM()) 只会废掉索引。
        rows = DownloadTask.select(DownloadTask.info_hash).where(
            (DownloadTask.movie == movie_number.strip()) & download_task_dead_expression()
        )
        dead_hashes: set[str] = set()
        for row in rows:
            try:
                dead_hashes.add(canonicalize_btih(row.info_hash))
            except ValueError:
                # 下载器写回的 hash 理应是 40 位 hex，出现异常值时无法参与比较，记日志跳过。
                logger.warning(
                    "Skip unparseable download task info_hash movie_number={} info_hash={}",
                    movie_number,
                    row.info_hash,
                )
        return dead_hashes

    @staticmethod
    def _is_cloud115_preferred() -> bool:
        """全局下载器偏好首项是否为 115：决定本轮选种走「网盘优先」策略。"""
        preferred_kinds = settings.downloads.preferred_client_kinds or []
        return bool(preferred_kinds) and preferred_kinds[0] == DownloadClientKind.CLOUD115.value

    def _pick_best_candidate(
        self,
        candidates: Sequence[DownloadCandidateResource],
        excluded_info_hashes: set[str] | None = None,
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
        sorted_candidates = sorted(candidate_pool, key=self._candidate_sort_key)

        if not excluded_info_hashes:
            return sorted_candidates[0]

        # 候选的 info_hash 在解析索引器响应时就已确定（torznab infohash 属性 / 磁力链），
        # 这里纯内存比对，不产生任何网络请求。身份未知的候选照常放行：它可能压根不是死种，
        # 为一个不确定的判断去拉 .torrent 文件不值得——真是死种的话，下一轮它带着
        # DownloadTask 行回来，那时身份就是确定的了。
        for candidate in sorted_candidates:
            if candidate.info_hash and candidate.info_hash in excluded_info_hashes:
                continue
            return candidate
        return None

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
