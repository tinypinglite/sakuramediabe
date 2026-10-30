"""Submit a candidate through the download component of its provider bundle."""

from __future__ import annotations

from src.api.exception.errors import ApiError
from src.common.media_import_status import IMPORT_STATUS_PENDING
from src.common.movie_numbers import movie_number_lookup_values, normalize_movie_number
from src.model import (
    DownloadResourceBlacklist,
    DownloadSubmissionRecord,
    DownloadTask,
    Movie,
    VideoCollection,
)
from src.model.base import get_database
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    DownloadSubmission,
    ProviderOperationError,
    RemoteDownloadTask,
)
from src.schema.transfers.downloads import (
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
)
from src.service.transfers.downloads.common import (
    download_provider,
    list_indexer_clients,
    require_client,
    require_indexer,
    resolve_preferred_client,
    validate_non_empty,
    validate_remote_download_task,
)
from src.service.transfers.downloads.resource_hash import (
    resolve_magnet,
    resolve_resource_hash,
    resolve_torrent_upload,
)


class DownloadRequestService:
    def create_request(self, payload: DownloadRequestCreateRequest) -> DownloadRequestCreateResponse:
        client = self._resolve_client(payload)
        movie_number = validate_non_empty(
            payload.movie_number,
            "invalid_download_request_movie_number",
            "movie_number cannot be empty",
        )
        source_uri = validate_non_empty(
            payload.candidate.source_uri,
            "invalid_download_request_candidate",
            "candidate source_uri cannot be empty",
        )
        candidate_title = validate_non_empty(
            payload.candidate.title,
            "invalid_download_request_candidate",
            "candidate title cannot be empty",
        )
        info_hash = resolve_resource_hash(source_uri)
        display_name = self._provider_display_name(movie_number, info_hash)
        if DownloadResourceBlacklist.select().where(
            DownloadResourceBlacklist.info_hash == info_hash
        ).exists():
            raise ApiError(422, "download_source_blacklisted", "该资源已被拉黑")
        record, remote_task = self._submit_source(
            client,
            movie_number=movie_number,
            indexer_name=payload.candidate.indexer_name,
            title=candidate_title,
            release_title=payload.candidate.release_title,
            description=payload.candidate.description,
            source_uri=source_uri,
            info_hash=info_hash,
            display_name=display_name,
        )
        with get_database().atomic():
            task, created = DownloadTask.get_or_create(
                client=client,
                remote_id=remote_task.remote_id,
                defaults={
                    "movie": movie_number,
                    "name": remote_task.name or display_name,
                    "state": remote_task.state,
                    "progress": remote_task.progress,
                    "completed_source_ref": remote_task.completed_source_ref,
                    "import_status": "pending",
                },
            )
            if not created:
                task.movie = movie_number
                task.name = remote_task.name or display_name
                task.state = remote_task.state
                task.progress = remote_task.progress
                task.completed_source_ref = remote_task.completed_source_ref
                task.save(
                    only=[
                        DownloadTask.movie,
                        DownloadTask.name,
                        DownloadTask.state,
                        DownloadTask.progress,
                        DownloadTask.completed_source_ref,
                    ]
                )
            record.task_id = task.id
            record.save(only=[DownloadSubmissionRecord.task_id])
            if created:
                # 只有真正新建远端任务才算补库请求被受理；复用已有任务既不会重新下载，
                # 也不会重跑导入，标记留着让调度换下一个候选。
                Movie.update(media_replacement_pending=False).where(
                    Movie.movie_number == movie_number,
                    Movie.media_replacement_pending == True,
                ).execute()
        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )

    def create_manual_request(
        self,
        *,
        client_id: int,
        media_kind: str,
        source_uri: str | None = None,
        torrent_bytes: bytes | None = None,
        movie_number: str | None = None,
        collection_id: int | None = None,
        display_name: str | None = None,
        delete_source_after_import: bool = False,
    ) -> DownloadRequestCreateResponse:
        """用户手动提交磁力链或上传种子；不经过索引器，下载器必选。"""
        if media_kind not in {"jav", "video"}:
            raise ApiError(422, "invalid_media_kind", "无效的媒体类型")
        # 先校验纯输入，再查库，错误顺序与用户填写顺序一致。
        if media_kind == "jav":
            movie_number = self._manual_movie_number(movie_number)
        else:
            movie_number = None
        client = require_client(client_id)
        if collection_id is not None:
            if media_kind != "video":
                raise ApiError(
                    422, "invalid_collection", "jav import does not support collection_id"
                )
            if VideoCollection.get_or_none(VideoCollection.id == collection_id) is None:
                raise ApiError(
                    404,
                    "video_collection_not_found",
                    "视频合集不存在",
                    {"collection_id": collection_id},
                )

        if torrent_bytes is not None:
            info_hash, torrent_name = resolve_torrent_upload(torrent_bytes)
            source_uri = f"magnet:?xt=urn:btih:{info_hash}"
            supports_torrent_bytes = MEDIA_PROVIDER_REGISTRY.supports_torrent_bytes(
                client.library.provider_key
            )
        else:
            source_uri = (source_uri or "").strip()
            info_hash, torrent_name = resolve_magnet(source_uri)
            supports_torrent_bytes = False

        if DownloadResourceBlacklist.select().where(
            DownloadResourceBlacklist.info_hash == info_hash
        ).exists():
            raise ApiError(422, "download_source_blacklisted", "该资源已被拉黑")

        existing_task_id = (
            DownloadSubmissionRecord.select(DownloadSubmissionRecord.task_id)
            .where(
                (DownloadSubmissionRecord.client_id == client.id)
                & (DownloadSubmissionRecord.info_hash == info_hash)
                & (DownloadSubmissionRecord.task_id.is_null(False))
            )
            .order_by(DownloadSubmissionRecord.id.desc())
            .scalar()
        )
        if existing_task_id is not None:
            existing_task = DownloadTask.get_or_none(DownloadTask.id == existing_task_id)
            if (
                existing_task is not None
                and existing_task.import_status != IMPORT_STATUS_PENDING
            ):
                raise ApiError(
                    409,
                    "download_task_already_imported",
                    "该资源已有下载任务且已进入导入流程",
                    {"task_id": existing_task.id},
                )

        requested_name = (display_name or "").strip()[:255]
        base_name = requested_name or torrent_name or movie_number or info_hash[:8]
        provider_display_name = self._provider_display_name(base_name, info_hash)
        record, remote_task = self._submit_source(
            client,
            movie_number=movie_number or "",
            indexer_name="manual",
            title=base_name,
            release_title=torrent_name,
            description=None,
            source_uri=source_uri,
            info_hash=info_hash,
            display_name=provider_display_name,
            torrent_bytes=torrent_bytes if supports_torrent_bytes else None,
        )

        task_fields = {
            "movie": movie_number,
            "media_kind": media_kind,
            "collection_id": collection_id,
            "delete_source_after_import": bool(delete_source_after_import),
            "name": remote_task.name or provider_display_name,
            "state": remote_task.state,
            "progress": remote_task.progress,
            "completed_source_ref": remote_task.completed_source_ref,
        }
        with get_database().atomic():
            task = DownloadTask.get_or_none(
                (DownloadTask.client == client)
                & (DownloadTask.remote_id == remote_task.remote_id)
            )
            if task is not None and task.import_status != IMPORT_STATUS_PENDING:
                record.state = "failed"
                record.error_code = "download_task_already_imported"
                record.save()
                raise ApiError(
                    409,
                    "download_task_already_imported",
                    "该资源已有下载任务且已进入导入流程",
                    {"task_id": task.id},
                )
            if task is None:
                task = DownloadTask.create(
                    client=client,
                    remote_id=remote_task.remote_id,
                    import_status=IMPORT_STATUS_PENDING,
                    display_name=requested_name or None,
                    **task_fields,
                )
                created = True
            else:
                # 重提同一资源且未填写名字时保留上一次的用户命名。
                task_fields["display_name"] = requested_name or task.display_name
                for field, value in task_fields.items():
                    setattr(task, field, value)
                task.save(only=[getattr(DownloadTask, field) for field in task_fields])
                created = False
            record.task_id = task.id
            record.save(only=[DownloadSubmissionRecord.task_id])
            if created and media_kind == "jav" and movie_number:
                Movie.update(media_replacement_pending=False).where(
                    Movie.movie_number == movie_number,
                    Movie.media_replacement_pending == True,
                ).execute()
        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )

    def _submit_source(
        self,
        client,
        *,
        movie_number: str,
        indexer_name: str,
        title: str,
        release_title: str | None,
        description: str | None,
        source_uri: str,
        info_hash: str,
        display_name: str,
        torrent_bytes: bytes | None = None,
    ) -> tuple[DownloadSubmissionRecord, RemoteDownloadTask]:
        record = DownloadSubmissionRecord.create(
            client_id=client.id,
            movie_number=movie_number,
            indexer_name=indexer_name,
            title=title,
            release_title=(release_title or "").strip() or None,
            description=(description or "").strip() or None,
            source_uri=source_uri,
            info_hash=info_hash,
        )
        try:
            remote_task = download_provider(client).submit(
                submission=DownloadSubmission(
                    source_uri=source_uri,
                    display_name=display_name,
                    torrent_bytes=torrent_bytes,
                )
            )
            remote_task = validate_remote_download_task(remote_task)
        except Exception as exc:
            record.state = "failed"
            record.error_code = (
                exc.code
                if isinstance(exc, (ApiError, ProviderOperationError))
                else type(exc).__name__
            )
            record.save()
            if isinstance(exc, ProviderOperationError):
                raise self._provider_error(exc) from exc
            raise
        record.state = "submitted"
        record.remote_id = remote_task.remote_id
        record.save()
        return record, remote_task

    @staticmethod
    def _manual_movie_number(value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            raise ApiError(
                422,
                "invalid_download_request_movie_number",
                "JAV 手动下载必须填写番号",
            )
        candidates = movie_number_lookup_values(raw)
        movie = (
            Movie.get_or_none(Movie.movie_number.in_(candidates))
            if candidates
            else None
        )
        if movie is not None:
            return movie.movie_number
        normalized = normalize_movie_number(raw)
        if not normalized:
            raise ApiError(
                422,
                "invalid_download_request_movie_number",
                "JAV 手动下载必须填写番号",
            )
        return normalized

    @staticmethod
    def _provider_display_name(base: str, info_hash: str) -> str:
        # 115 以 <name>-<hash6> 识别托管目录；qB 也沿用同一目录命名。
        # 先截名字再加后缀，保证总长不超过 display_name 列的 255。
        suffix = info_hash[:6]
        name = " ".join((base or "").split())[:248] or info_hash[:8]
        if name.lower().endswith(f"-{suffix}"):
            return name
        return f"{name}-{suffix}"

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "task_not_managed": 409,
            "source_blacklisted": 422,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )

    @staticmethod
    def _resolve_client(payload: DownloadRequestCreateRequest):
        indexer = require_indexer(payload.candidate.indexer_name)
        clients = list_indexer_clients(indexer)
        if payload.client_id is None:
            return resolve_preferred_client(clients)
        client = require_client(payload.client_id)
        if any(bound_client.id == client.id for bound_client in clients):
            return client
        raise ApiError(
            422,
            "download_request_client_not_bound_to_indexer",
            "Download client is not bound to candidate indexer",
            {"client_id": client.id, "indexer_name": indexer.name},
        )
