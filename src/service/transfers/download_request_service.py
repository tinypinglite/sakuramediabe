from loguru import logger

from src.api.exception.errors import ApiError
from src.model import DownloadTask
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
)
from src.service.transfers.cloud115_offline_service import (
    Cloud115OfflineDownloadService,
)
from src.service.transfers.common import (
    build_movie_save_path,
    list_indexer_clients,
    map_remote_path,
    require_client,
    require_indexer,
    resolve_preferred_client,
    validate_non_empty,
)
from src.service.transfers.qbittorrent_client import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.torrent_content_guard import (
    assert_candidate_content_importable,
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
        assert_candidate_content_importable(
            title=payload.candidate.title,
            torrent_url=(payload.candidate.torrent_url or "").strip(),
            magnet_url=(payload.candidate.magnet_url or "").strip(),
        )

        # 按下载入口种类分派；选中即执行到底，执行失败直接报错、不自动换下载器。
        if client.kind == DownloadClientKind.CLOUD115.value:
            return self._create_cloud115_request(client, movie_number, payload)
        return self._create_qbittorrent_request(client, movie_number, payload)

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

    def _create_qbittorrent_request(
        self, client, movie_number: str, payload: DownloadRequestCreateRequest
    ) -> DownloadRequestCreateResponse:
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
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
            task.movie = movie_number
            task.name = remote_task.get("name") or payload.candidate.title
            task.save_path = map_remote_path(
                client,
                remote_task.get("save_path") or movie_save_path,
            )
            task.progress = remote_task.get("progress", 0.0)
            task.download_state = "queued"
            task.save()

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
