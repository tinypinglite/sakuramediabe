from src.api.exception.errors import ApiError
from src.model import DownloadTask
from src.schema.transfers.downloads import (
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
)
from src.service.transfers.common import map_remote_path, require_client, validate_non_empty
from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError


class DownloadRequestService:
    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    def create_request(self, payload: DownloadRequestCreateRequest) -> DownloadRequestCreateResponse:
        client = require_client(payload.client_id)
        movie_number = validate_non_empty(
            payload.movie_number,
            "invalid_download_request_movie_number",
            "movie_number cannot be empty",
        ).upper()
        if not ((payload.candidate.magnet_url or "").strip() or (payload.candidate.torrent_url or "").strip()):
            raise ApiError(
                422,
                "invalid_download_request_candidate",
                "candidate must provide magnet_url or torrent_url",
            )

        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        try:
            remote_task = qb_client.add_candidate(
                magnet_url=(payload.candidate.magnet_url or "").strip(),
                torrent_url=(payload.candidate.torrent_url or "").strip(),
                save_path=client.client_save_path,
                rename=movie_number,
                client_id=client.id,
            )
        except QBittorrentClientError as exc:
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
                "save_path": map_remote_path(client, remote_task.get("save_path") or client.client_save_path),
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
                remote_task.get("save_path") or client.client_save_path,
            )
            task.progress = remote_task.get("progress", 0.0)
            task.download_state = "queued"
            task.save()

        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )
