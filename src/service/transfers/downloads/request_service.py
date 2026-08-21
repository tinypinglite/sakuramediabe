from loguru import logger

from src.api.exception.errors import ApiError
from src.model import DownloadTask
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
)
from src.service.transfers.cloud115.offline.service import (
    Cloud115OfflineDownloadService,
)
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.downloads.common import (
    build_movie_save_path,
    list_indexer_clients,
    map_remote_path,
    require_client,
    require_indexer,
    resolve_preferred_client,
    validate_non_empty,
)
from src.service.transfers.downloads.guards.torrent_content_guard import (
    assert_candidate_content_importable,
)
from src.service.transfers.shared.common import (
    DOWNLOAD_DEAD_STATES,
    ERROR_CODE_CANDIDATE_DEAD,
    canonicalize_btih,
    download_task_dead_expression,
)


class DownloadRequestService:
    def __init__(
        self,
        qbittorrent_client_cls=QBittorrentClient,
        cloud115_offline_service: Cloud115OfflineDownloadService | None = None,
    ):
        self.qbittorrent_client_cls = qbittorrent_client_cls
        self.cloud115_offline_service = cloud115_offline_service or Cloud115OfflineDownloadService()

    def create_request(self, payload: DownloadRequestCreateRequest) -> DownloadRequestCreateResponse:
        client = self._resolve_client(payload)
        # 入参是影片页传来的规范番号（provider 原样），不做 upper——东热 n0646 这类小写番号
        # 一旦改写，落库的 task.movie_number 就与 Movie.movie_number 对不上，JOIN 全部失配。
        movie_number = validate_non_empty(
            payload.movie_number,
            "invalid_download_request_movie_number",
            "movie_number cannot be empty",
        )
        if not ((payload.candidate.magnet_url or "").strip() or (payload.candidate.torrent_url or "").strip()):
            raise ApiError(
                422,
                "invalid_download_request_candidate",
                "candidate must provide magnet_url or torrent_url",
            )

        # 内容闸门放在分派之前：qB 与 115 共用本入口，自动下载与手动提交也都走这里，
        # 拦在这一层才能保证"不可导入的资源永远不会被真正提交出去"。
        # torrent-only 候选的身份只在这次 .torrent 解析里廉价可得，一并取出来做死种黑名单比对。
        resolved_info_hash = assert_candidate_content_importable(
            movie_number=movie_number,
            title=payload.candidate.title,
            torrent_url=(payload.candidate.torrent_url or "").strip(),
            magnet_url=(payload.candidate.magnet_url or "").strip(),
        )
        info_hash = self._resolve_effective_info_hash(
            candidate_info_hash=(payload.candidate.info_hash or "").strip(),
            resolved_info_hash=resolved_info_hash,
            candidate_title=payload.candidate.title,
        )
        self._ensure_candidate_not_dead(movie_number, info_hash)

        # 按下载入口种类分派；选中即执行到底，执行失败直接报错、不自动换下载器。
        if client.kind == DownloadClientKind.CLOUD115.value:
            return self._create_cloud115_request(client, movie_number, payload)
        return self._create_qbittorrent_request(client, movie_number, payload, info_hash=info_hash)

    def _create_cloud115_request(
        self, client, movie_number: str, payload: DownloadRequestCreateRequest
    ) -> DownloadRequestCreateResponse:
        task, created = self.cloud115_offline_service.submit_candidate(
            client,
            movie_number=movie_number,
            candidate=payload.candidate,
        )
        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )

    @staticmethod
    def _resolve_effective_info_hash(
        *,
        candidate_info_hash: str,
        resolved_info_hash: str,
        candidate_title: str,
    ) -> str:
        """合并候选自带 hash 与 .torrent 解析结果，返回 canonical 40 位小写 hex。

        实际 .torrent 字节里的 hash 优先：torznab 属性可能与真实种子不一致，字节才是权威。
        """
        raw_hash = resolved_info_hash or candidate_info_hash
        if not raw_hash:
            raise ApiError(
                422,
                "download_candidate_identity_missing",
                "无法确定候选种子身份",
                {"candidate_title": candidate_title},
            )
        try:
            return canonicalize_btih(raw_hash)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_download_request_candidate",
                "候选种子 hash 无法解析",
                {"detail": str(exc)},
            ) from exc

    @staticmethod
    def _ensure_candidate_not_dead(movie_number: str, info_hash: str) -> None:
        """提交前死种黑名单兜底：命中直接拒绝，绝不把死态台账行复活成 queued。"""
        dead_task = (
            DownloadTask.select()
            .where(
                DownloadTask.movie == movie_number,
                DownloadTask.info_hash == info_hash,
                download_task_dead_expression(),
            )
            .limit(1)
            .get_or_none()
        )
        if dead_task is None:
            return
        raise ApiError(
            409,
            ERROR_CODE_CANDIDATE_DEAD,
            "该种子已判死，不会重复提交；如需重试请先删除原下载任务",
            {
                "movie_number": movie_number,
                "info_hash": info_hash,
                "download_task_id": dead_task.id,
            },
        )

    def _create_qbittorrent_request(
        self,
        client,
        movie_number: str,
        payload: DownloadRequestCreateRequest,
        *,
        info_hash: str,
    ) -> DownloadRequestCreateResponse:
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        # create_request 已预检过，这里再兜一道：并发/其它入口也不能把死行改成 queued。
        self._ensure_candidate_not_dead(movie_number, info_hash)
        # 按番号给每个种子指定独立子目录落盘，避免内容平铺到下载根目录、导致自动导入误扫整根。
        movie_save_path = build_movie_save_path(client.client_save_path, movie_number)
        try:
            remote_task = qb_client.add_candidate(
                magnet_url=(payload.candidate.magnet_url or "").strip(),
                torrent_url=(payload.candidate.torrent_url or "").strip(),
                save_path=movie_save_path,
                rename=movie_number,
                client_id=client.id,
            )
        except QBittorrentClientError as exc:
            # qBittorrent 添加种子失败时底层异常会被包成 502，这里先记日志，避免真实报错只存在于响应体而服务端无迹可查
            source = "magnet" if (payload.candidate.magnet_url or "").strip() else "torrent_url"
            logger.warning(
                "download request failed: movie_number={} client_id={} source={} error={}",
                movie_number,
                client.id,
                source,
                exc,
            )
            raise ApiError(
                502,
                "download_request_failed",
                "qBittorrent request failed",
                {"detail": str(exc)},
            ) from exc

        task, created = DownloadTask.get_or_create(
            client=client,
            info_hash=remote_task["info_hash"],
            defaults={
                "movie": movie_number,
                "name": remote_task.get("name") or payload.candidate.title,
                "save_path": map_remote_path(client, remote_task.get("save_path") or movie_save_path),
                "progress": remote_task.get("progress", 0.0),
                "download_state": "queued",
                "import_status": "pending",
            },
        )
        if not created:
            if task.download_state in DOWNLOAD_DEAD_STATES:
                # 预检到 get_or_create 之间出现死态写入（理论竞态）：宁可报错也不复活黑名单行。
                raise ApiError(
                    409,
                    ERROR_CODE_CANDIDATE_DEAD,
                    "该种子已被判死，任务未被复活；清理或重试请先处理原下载任务",
                    {"info_hash": remote_task["info_hash"], "download_task_id": task.id},
                )
            task.movie = movie_number
            task.name = remote_task.get("name") or payload.candidate.title
            task.save_path = map_remote_path(
                client,
                remote_task.get("save_path") or movie_save_path,
            )
            task.progress = remote_task.get("progress", 0.0)
            task.download_state = "queued"
            # 重复提交只收口任务元数据，不能覆盖后台进度采样写入的快照列。
            task.save(
                only=[
                    DownloadTask.movie,
                    DownloadTask.name,
                    DownloadTask.save_path,
                    DownloadTask.progress,
                    DownloadTask.download_state,
                ]
            )

        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )

    def _resolve_client(self, payload: DownloadRequestCreateRequest):
        indexer = require_indexer(payload.candidate.indexer_name)
        clients = list_indexer_clients(indexer)
        # 显式 client_id 只能在当前索引器绑定集合内覆盖，避免绕过索引器种类约束。
        if payload.client_id is not None:
            client = require_client(payload.client_id)
            if all(bound_client.id != client.id for bound_client in clients):
                raise ApiError(
                    422,
                    "download_request_client_not_bound_to_indexer",
                    "Download client is not bound to candidate indexer",
                    {
                        "client_id": client.id,
                        "indexer_name": indexer.name,
                    },
                )
            return client

        if not clients:
            raise ApiError(
                422,
                "download_request_client_resolution_failed",
                "Indexer has no bound download clients",
                {"indexer_name": indexer.name},
            )
        return resolve_preferred_client(clients)
