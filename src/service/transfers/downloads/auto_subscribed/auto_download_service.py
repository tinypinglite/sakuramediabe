from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, NoReturn

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.service_helpers import media_exists_expression, rest_between_requests
from src.model import DownloadTask, Movie
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadCandidateCreatePayload,
    DownloadCandidateResource,
    DownloadRequestCreateRequest,
)
from src.service.catalog.movie_subscription_search_state_service import (
    ERROR_CODE_NO_CANDIDATE,
    MovieSubscriptionSearchStateService,
    SubscriptionSearchError,
)
from src.service.transfers.downloads.guards.tag_rules import SUBTITLE_TAG
from src.service.transfers.downloads.guards.torrent_content_guard import (
    ERROR_CODE_CONTENT_REJECTED,
    ERROR_CODE_CONTENT_UNVERIFIABLE,
)
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.search_service import DownloadSearchService
from src.service.transfers.shared.common import (
    ERROR_CODE_CANDIDATE_DEAD,
    active_download_task_exists_expression,
    canonicalize_btih,
    download_task_dead_expression,
)

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
# 一部影片本轮最多因内容闸门/死种确认换几次种。资源池里原盘通常只占头部一两条，给到 5 已经很宽；
# 设上限是为了兜住"整池全是坏候选"的病态情形——否则会把几十个候选逐个拉一遍种子文件。
MAX_REJECTED_CANDIDATES = 5
TASK_KEY = "subscribed_movie_auto_download"
# 内容闸门与死种黑名单给出的拒绝，在换种这件事上等价处理（区别只体现在给调用方的 HTTP 语义上）。
CANDIDATE_REJECTION_ERROR_CODES = frozenset(
    {
        ERROR_CODE_CONTENT_REJECTED,
        ERROR_CODE_CONTENT_UNVERIFIABLE,
        ERROR_CODE_CANDIDATE_DEAD,
    }
)


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

    def _setup_run(self) -> dict[str, Any]:
        return {
            # cloud115 下载入口 id 集合：判断某次提交是否打了 115，决定要不要为下一个排队等待。
            "cloud115_client_ids": self._cloud115_client_ids(),
            "pending_cloud115_rest": False,
            "searched_movies": 0,
            "submitted_movies": 0,
            "no_candidate_movies": 0,
            "skipped_movies": 0,
            "content_rejected_candidates": 0,
            "dead_rejected_candidates": 0,
            "submitted_movie_numbers": [],
            "no_candidate_movie_numbers": [],
            "failed_items": [],
        }

    def _process_one(self, shared: dict[str, Any], movie: Movie) -> None:
        movie_number = movie.movie_number
        shared["searched_movies"] += 1
        logger.info("Auto download searching candidates for movie_number={}", movie_number)
        try:
            candidates = self.download_search_service.search_candidates(movie_number=movie_number)
        except Exception as exc:
            shared["failed_items"].append(
                {"movie_number": movie_number, "stage": "search", "detail": str(exc)}
            )
            logger.exception(
                "Auto download candidate search failed movie_number={} detail={}",
                movie_number,
                exc,
            )
            raise SubscriptionSearchError(
                "indexer_search_failed", str(exc), consumes_budget=False
            ) from exc

        # 该影片已经试过并判死的种子：重查时必须排除，否则确定性排序会把同一个死种反复选中。
        excluded_info_hashes = self._list_dead_info_hashes(movie_number)
        # 本轮被内容闸门/死种黑名单拒绝的候选。PT 索引器的 torznab 响应可能既不给 infohash
        # 也不给磁力，候选身份恒为空串，只能靠提交链路解析 .torrent 后确认是否死种。
        # 拒绝记录只在本轮内有效——跨轮记忆需要持久化台账，等这个闸门跑稳再说。
        rejected_keys: set[tuple] = set()
        content_rejected_candidates = 0
        dead_rejected_candidates = 0
        response = None
        for _ in range(MAX_REJECTED_CANDIDATES):
            candidate = self._pick_best_candidate(
                [
                    item
                    for item in candidates
                    if self._candidate_key(item) not in rejected_keys
                ],
                excluded_info_hashes,
            )
            if candidate is None:
                break

            payload = DownloadRequestCreateRequest(
                movie_number=movie_number,
                candidate=self._build_candidate_payload(candidate),
            )
            # 上一次提交打过 115 时，先歇一会儿再发下一个（只在真正要提交前等，
            # 查不到候选的影片不碰 115，不该白等）。
            if shared["pending_cloud115_rest"]:
                delay = rest_between_requests(SUBMIT_REST_MIN_SECONDS, SUBMIT_REST_MAX_SECONDS)
                logger.info(
                    "Auto download resting before next cloud115 submit "
                    "movie_number={} delay_seconds={:.1f}",
                    movie_number,
                    delay,
                )
                time.sleep(delay)
                shared["pending_cloud115_rest"] = False
            try:
                response = self.download_request_service.create_request(payload)
                break
            except ApiError as exc:
                if exc.code not in CANDIDATE_REJECTION_ERROR_CODES:
                    self._raise_submit_failed(shared, movie_number, candidate, exc)
                # 内容闸门/死种黑名单在分派下载器之前就拦下了，本次没有碰过 115，不需要补休息。
                #
                # 校验不了（拿不到 .torrent）、内容不合格、确认是死种在这里一视同仁地换种，
                # 不能因头部坏候选中止本影片；候选耗尽后本轮记为 no_candidate。
                # 索引器整体故障不会走到这里——那种情况 search_candidates 先失败。
                rejected_keys.add(self._candidate_key(candidate))
                if exc.code == ERROR_CODE_CANDIDATE_DEAD:
                    shared["dead_rejected_candidates"] += 1
                    dead_rejected_candidates += 1
                    logger.info(
                        "Auto download candidate rejected as dead "
                        "movie_number={} title={} size_bytes={} detail={}",
                        movie_number,
                        candidate.title,
                        candidate.size_bytes,
                        exc.details,
                    )
                else:
                    shared["content_rejected_candidates"] += 1
                    content_rejected_candidates += 1
                    logger.info(
                        "Auto download candidate rejected by content guard "
                        "movie_number={} title={} size_bytes={} code={} detail={}",
                        movie_number,
                        candidate.title,
                        candidate.size_bytes,
                        exc.code,
                        exc.details,
                    )
            except Exception as exc:
                self._raise_submit_failed(shared, movie_number, candidate, exc)

        if response is None:
            shared["no_candidate_movies"] += 1
            shared["no_candidate_movie_numbers"].append(movie_number)
            logger.info(
                "Auto download found no usable candidate movie_number={} "
                "excluded_dead={} confirmed_dead={} content_rejected={}",
                movie_number,
                len(excluded_info_hashes),
                dead_rejected_candidates,
                content_rejected_candidates,
            )
            raise SubscriptionSearchError(
                ERROR_CODE_NO_CANDIDATE,
                f"未找到可用资源（已排除死种 {len(excluded_info_hashes)} 个、"
                f"已确认死种 {dead_rejected_candidates} 个、内容不合格 "
                f"{content_rejected_candidates} 个）",
                consumes_budget=True,
            )

        # 只有真正向 115 发过请求才需要为下一个排队等待：qB 提交不碰 115；
        # created=False 说明本地已有同 (client, info_hash) 的任务、提交直接短路返回。
        shared["pending_cloud115_rest"] = response.created and self._is_cloud115_task(
            response.task.client_id, shared["cloud115_client_ids"]
        )
        if response.created:
            shared["submitted_movies"] += 1
            shared["submitted_movie_numbers"].append(movie_number)
            logger.info(
                "Auto download submitted movie_number={} title={} info_hash={}",
                movie_number,
                response.task.name,
                response.task.info_hash,
            )
            return
        shared["skipped_movies"] += 1
        logger.info(
            "Auto download skipped existing request movie_number={} title={}",
            movie_number,
            candidate.title,
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
            failed_before = len(shared["failed_items"])
            try:
                self._process_one(shared, movie)
                MovieSubscriptionSearchStateService.mark_succeeded(movie.id)
            except SubscriptionSearchError as exc:
                MovieSubscriptionSearchStateService.mark_failed(movie, exc)
            except Exception as exc:
                # 搜索/提交链路会记录精确阶段；候选转换、响应解析等意外异常统一记 process。
                if len(shared["failed_items"]) == failed_before:
                    shared["failed_items"].append(
                        {
                            "movie_number": movie.movie_number,
                            "stage": "process",
                            "detail": str(exc),
                        }
                    )
                logger.exception(
                    "Auto download item failed movie_id={} movie_number={}",
                    movie.id,
                    movie.movie_number,
                )
                MovieSubscriptionSearchStateService.mark_failed(
                    movie,
                    SubscriptionSearchError(
                        "subscription_search_process_failed",
                        str(exc),
                        consumes_budget=False,
                    ),
                )
            reporter.emit(current=current, total=total)
        return {
            "candidate_movies": len(candidate_ids),
            "searched_movies": shared.get("searched_movies", 0),
            "submitted_movies": shared.get("submitted_movies", 0),
            "no_candidate_movies": shared.get("no_candidate_movies", 0),
            "content_rejected_candidates": shared.get("content_rejected_candidates", 0),
            "dead_rejected_candidates": shared.get("dead_rejected_candidates", 0),
            "skipped_movies": shared.get("skipped_movies", 0),
            "failed_movies": len(shared.get("failed_items", [])),
            "submitted_movie_numbers": shared.get("submitted_movie_numbers", []),
            "no_candidate_movie_numbers": shared.get("no_candidate_movie_numbers", []),
            "failed_items": shared.get("failed_items", []),
        }

    _media_exists_expression = staticmethod(media_exists_expression)

    def _select_candidate_ids(self) -> list[int]:
        """候选由领域事实和订阅搜索预算共同决定。"""
        from src.common.runtime_time import utc_now_for_db

        query = (
            Movie.select(Movie.id)
            .where(Movie.is_subscribed == True)
            .where(~self._media_exists_expression())
            .where(~active_download_task_exists_expression())
            .where(MovieSubscriptionSearchStateService.candidate_condition(now=utc_now_for_db()))
            .order_by(Movie.subscribed_at.asc(), Movie.id.asc())
        )
        return [int(movie_id) for (movie_id,) in query.tuples()]

    @staticmethod
    def _list_dead_info_hashes(movie_number: str) -> set[str]:
        """取该影片所有已判死的下载任务 info_hash，作为本轮选种黑名单。

        DownloadTask 本身就是台账，因此黑名单不需要额外的表或字段。黑名单是永久的：info_hash
        内容寻址，同一个 hash 就是同一个 swarm，换索引器它照样是死的。要重试某个具体种子，
        手动删除该下载任务（UI 删任务会同步删 qB 侧与本地行）；仅凭在 qB 里删掉种子不会解除
        黑名单——_prune_ghost_tasks 对死态行豁免（停滞清理的闭环前提，见 sync_service）。
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
    def _candidate_key(candidate: DownloadCandidateResource) -> tuple:
        """本轮内标识一个候选。

        不用 info_hash：PT 索引器的 torznab 响应可能不带 infohash 也不带磁力，候选身份恒为
        空串，身份确认交给提交阶段内容闸门解析 .torrent 后完成。索引器名 + 标题 + 体积在
        一次搜索结果里足以区分，也与 ``_candidate_sort_key`` 的确定性排序口径一致。
        """
        return (candidate.indexer_name, candidate.title, candidate.size_bytes)

    @staticmethod
    def _raise_submit_failed(shared, movie_number: str, candidate, exc) -> NoReturn:
        """提交失败的统一出口：写入本次 TaskRun 汇总后中止当前影片。"""
        # 提交失败时无从判断是否已经打到 115（"建目录成功、离线提交失败" 也走这条路径），
        # 而 WAF 一旦触发正是最不能连打的时刻，故一律按"打过 115"记账、下一部先休息。
        shared["pending_cloud115_rest"] = True
        shared["failed_items"].append(
            {"movie_number": movie_number, "stage": "submit", "detail": str(exc)}
        )
        logger.exception(
            "Auto download submit failed movie_number={} title={} detail={}",
            movie_number,
            candidate.title,
            exc,
        )
        raise SubscriptionSearchError(
            "download_submit_failed", str(exc), consumes_budget=False
        ) from exc

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
            # 已知身份的候选（torznab infohash / 磁力链）纯内存比对，不产生网络请求。
            # 身份未知的 torrent-only 候选照常放行，提交链路会在内容闸门解析 .torrent 后
            # 确认死种并换下一个，避免选种阶段逐个拉文件。
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
            info_hash=candidate.info_hash,
            tags=list(candidate.tags or []),
        )
