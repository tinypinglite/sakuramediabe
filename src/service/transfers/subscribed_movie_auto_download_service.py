from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Sequence

from loguru import logger
from peewee import JOIN

from src.common.service_helpers import media_exists_expression
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
from src.service.transfers.tag_rules import SUBTITLE_TAG

MIN_SIZE_BYTES = 1 * 1024 * 1024 * 1024
MAX_SIZE_BYTES = 40 * 1024 * 1024 * 1024
# 打分时中字折算的字节加成：大小主导，中字只在两个候选差不多大时翻盘，
# 等价于「中字版最多容忍比无中字版小 2G 仍然优先」。
SUBTITLE_BONUS_BYTES = 2 * 1024 * 1024 * 1024
# 提交成功之间的随机停顿：cloud115 每次提交要打 1~2 个 webapi 请求（建任务目录），
# 而每部影片各自新建 SDK client，transport 的匀速闸门与批次计数都会随之归零——
# 也就是说跨影片**没有任何机制**在限速，实测提交间隔恒定约 3 秒。订阅积压时这就是
# 上百个连续 webapi 请求（实测连续 200 余次即触发 WAF 405），故在调用方补节奏。
# 与导入侧的番号间休息（MANUAL_GROUP_REST_*）保持同一量级。
SUBMIT_REST_MIN_SECONDS = 10.0
SUBMIT_REST_MAX_SECONDS = 30.0


class SubscribedMovieAutoDownloadService:
    def __init__(
        self,
        *,
        download_search_service: DownloadSearchService | None = None,
        download_request_service: DownloadRequestService | None = None,
    ):
        self.download_search_service = download_search_service or DownloadSearchService()
        self.download_request_service = download_request_service or DownloadRequestService()

    @staticmethod
    def _cloud115_client_ids() -> set[int]:
        from src.model import DownloadClient

        return {
            client.id
            for client in DownloadClient.select(DownloadClient.id).where(
                DownloadClient.kind == DownloadClientKind.CLOUD115.value
            )
        }

    @staticmethod
    def _is_cloud115_task(client_id: int | None, cloud115_client_ids: set[int]) -> bool:
        return client_id is not None and client_id in cloud115_client_ids

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

        # cloud115 下载入口 id 集合：判断某次提交是否打了 115，决定要不要为下一个排队等待。
        cloud115_client_ids = self._cloud115_client_ids()
        pending_cloud115_rest = False

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
            # 上一次提交打过 115 时，先歇一会儿再发下一个（只在真正要提交前等，
            # 查不到候选的影片不碰 115，不该白等）。
            if pending_cloud115_rest:
                delay = random.uniform(SUBMIT_REST_MIN_SECONDS, SUBMIT_REST_MAX_SECONDS)
                logger.info(
                    "Auto download resting before next cloud115 submit "
                    "movie_number={} delay_seconds={:.1f}",
                    movie_number,
                    delay,
                )
                time.sleep(delay)
                pending_cloud115_rest = False
            try:
                response = self.download_request_service.create_request(payload)
            except Exception as exc:
                # 提交失败时无从判断是否已经打到 115（"建目录成功、离线提交失败" 也走这条路径），
                # 而 WAF 一旦触发正是最不能连打的时刻，故一律按"打过 115"记账、下一部先休息。
                # 代价是磁力解析失败这类根本没碰 115 的失败也会多等一轮，换取失败不退化成连打。
                pending_cloud115_rest = True
                self._record_failure(summary, movie_number, stage="submit", detail=str(exc))
                SubscribedMovieSearchStateService.record_search_error(movie, str(exc))
                logger.exception(
                    "Auto download submit failed movie_number={} title={} detail={}",
                    movie_number,
                    candidate.title,
                    exc,
                )
                continue

            # 只有真正向 115 发过请求才需要为下一个排队等待：qB 提交不碰 115；
            # created=False 说明本地已有同 (client, info_hash) 的任务、提交直接短路返回，
            # 同样一个 115 请求都没发（115 侧已存在任务那条分支会新建本地记录，created=True）。
            pending_cloud115_rest = response.created and self._is_cloud115_task(
                response.task.client_id, cloud115_client_ids
            )

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

    def _pick_best_candidate(
        self,
        candidates: Sequence[DownloadCandidateResource],
        excluded_info_hashes: set[str] | None = None,
    ) -> DownloadCandidateResource | None:
        excluded = excluded_info_hashes or set()
        # 各过滤环节的击杀计数：全灭时打进日志，回答「订阅了为什么一直不下」。
        filtered_no_source = 0
        filtered_size = 0
        filtered_blacklist = 0
        filtered_seeders = 0
        pool: list[DownloadCandidateResource] = []
        for candidate in candidates:
            if not ((candidate.magnet_url or "").strip() or (candidate.torrent_url or "").strip()):
                filtered_no_source += 1
                continue
            if not (MIN_SIZE_BYTES <= candidate.size_bytes <= MAX_SIZE_BYTES):
                filtered_size += 1
                continue
            # 候选的 info_hash 在解析索引器响应时就已确定（torznab infohash 属性 / 磁力链），
            # 这里纯内存比对，不产生任何网络请求。身份未知的候选照常放行：它可能压根不是死种，
            # 为一个不确定的判断去拉 .torrent 文件不值得——真是死种的话，下一轮它带着
            # DownloadTask 行回来，那时身份就是确定的了。
            if candidate.info_hash and candidate.info_hash in excluded:
                filtered_blacklist += 1
                continue
            if candidate.seeders <= 0:
                filtered_seeders += 1
                continue
            pool.append(candidate)

        if not pool:
            logger.info(
                "Auto download candidates all filtered total={} no_source={} size_filtered={} "
                "blacklist_filtered={} seeders_filtered={}",
                len(candidates),
                filtered_no_source,
                filtered_size,
                filtered_blacklist,
                filtered_seeders,
            )
            return None
        return min(pool, key=self._candidate_sort_key)

    @staticmethod
    def _candidate_sort_key(candidate: DownloadCandidateResource) -> tuple:
        # 打分取最高：大小主导 + 中字小额加成（见 SUBTITLE_BONUS_BYTES）。同一个种子常被多个
        # 索引器同时返回而分数完全相同，(indexer_name, title) 兜底保证选种确定性——黑名单
        # 排除逻辑依赖「同一批候选每轮选出同一个」。
        score = candidate.size_bytes
        if SUBTITLE_TAG in (candidate.tags or []):
            score += SUBTITLE_BONUS_BYTES
        return (-score, candidate.indexer_name, candidate.title)

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
