import errno
import os
import time
import uuid
from pathlib import Path
from typing import List

from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask, Indexer
from src.schema.transfers.downloads import (
    DownloadClientCreateRequest,
    DownloadClientResource,
    DownloadClientStorageDirectoryMappingResult,
    DownloadClientStorageHardlinkResult,
    DownloadClientStorageTestError,
    DownloadClientStorageTestResponse,
    DownloadClientTestError,
    DownloadClientTestResponse,
    DownloadClientUpdateRequest,
)
from src.service.transfers.common import (
    ensure_name_available,
    require_client,
    require_media_library,
    validate_absolute_path,
    validate_base_url,
    validate_media_library_id,
    validate_non_empty,
)
from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError


class DownloadClientService:
    DIAGNOSTIC_DIR_NAME = ".sakuramedia-diagnostics"
    SENTINEL_FILENAME = "sentinel.txt"
    HARDLINK_FILENAME = "sentinel.link"

    @classmethod
    def list_clients(cls) -> List[DownloadClientResource]:
        clients = list(
            DownloadClient.select().order_by(
                DownloadClient.created_at.desc(),
                DownloadClient.id.desc(),
            )
        )
        return DownloadClientResource.from_models(clients)

    @classmethod
    def create_client(cls, payload: DownloadClientCreateRequest) -> DownloadClientResource:
        name = validate_non_empty(
            payload.name,
            "invalid_download_client_name",
            "Download client name cannot be empty",
        )
        username = validate_non_empty(
            payload.username,
            "invalid_download_client_username",
            "Download client username cannot be empty",
        )
        password = validate_non_empty(
            payload.password,
            "invalid_download_client_password",
            "Download client password cannot be empty",
        )
        media_library_id = validate_media_library_id(payload.media_library_id)
        require_media_library(media_library_id)
        ensure_name_available(name)

        client = DownloadClient.create(
            name=name,
            base_url=validate_base_url(payload.base_url),
            username=username,
            password=password,
            client_save_path=validate_absolute_path(
                payload.client_save_path,
                field_name="client_save_path",
            ),
            local_root_path=validate_absolute_path(
                payload.local_root_path,
                field_name="local_root_path",
            ),
            media_library=media_library_id,
        )
        return DownloadClientResource.from_model(client)

    @classmethod
    def test_client(
        cls,
        client_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadClientTestResponse:
        client = require_client(client_id)
        start_at = time.time()
        try:
            # 只检测 qB Web API 登录与版本接口，不读取种子、不写远端状态。
            status = qbittorrent_client_cls.from_download_client(client).inspect_status()
        except QBittorrentClientError as exc:
            return cls._build_test_response(
                client,
                start_at=start_at,
                healthy=False,
                error=DownloadClientTestError(
                    type="qbittorrent_request_error",
                    message=str(exc),
                ),
            )

        return cls._build_test_response(
            client,
            start_at=start_at,
            healthy=True,
            version=status.get("version"),
            web_api_version=status.get("web_api_version"),
        )

    @staticmethod
    def _build_test_response(
        client: DownloadClient,
        *,
        start_at: float,
        healthy: bool,
        version: str | None = None,
        web_api_version: str | None = None,
        error: DownloadClientTestError | None = None,
    ) -> DownloadClientTestResponse:
        return DownloadClientTestResponse(
            healthy=healthy,
            checked_at=utc_now_for_db(),
            client_id=client.id,
            client_name=client.name,
            base_url=client.base_url,
            elapsed_ms=int((time.time() - start_at) * 1000),
            version=version,
            web_api_version=web_api_version,
            error=error,
        )

    @classmethod
    def test_storage(
        cls,
        client_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
        probe_id: str | None = None,
    ) -> DownloadClientStorageTestResponse:
        client = require_client(client_id)
        start_at = time.time()
        probe_id = probe_id or uuid.uuid4().hex
        local_root = Path(client.local_root_path).expanduser()
        library_root = Path(client.media_library.root_path).expanduser()
        local_probe_dir = local_root / cls.DIAGNOSTIC_DIR_NAME / probe_id
        library_probe_dir = library_root / cls.DIAGNOSTIC_DIR_NAME / probe_id
        local_diagnostic_dir = local_probe_dir.parent
        library_diagnostic_dir = library_probe_dir.parent
        local_diagnostic_dir_existed = local_diagnostic_dir.exists()
        library_diagnostic_dir_existed = library_diagnostic_dir.exists()
        sentinel_path = local_probe_dir / cls.SENTINEL_FILENAME
        hardlink_path = library_probe_dir / cls.HARDLINK_FILENAME
        remote_probe_dir = cls._join_remote_path(client.client_save_path, cls.DIAGNOSTIC_DIR_NAME, probe_id)
        warnings: list[str] = []
        created_directories: list[Path] = []
        mapping = DownloadClientStorageDirectoryMappingResult(
            status="failed",
            client_save_path=client.client_save_path,
            local_root_path=client.local_root_path,
            probe_remote_dir=remote_probe_dir,
            probe_local_dir=str(local_probe_dir),
            sentinel_visible_to_qb=False,
            error=None,
        )
        hardlink = DownloadClientStorageHardlinkResult(
            status="skipped",
            supported=False,
            source_path=str(sentinel_path),
            target_path=str(hardlink_path),
            error=None,
        )

        try:
            if not local_root.exists() or not local_root.is_dir():
                mapping.error = cls._storage_error(
                    "local_root_path_not_accessible",
                    "local_root_path 不存在或不是目录",
                )
            else:
                # 哨兵文件放在专用诊断目录内，避免直接污染下载根目录。
                local_probe_dir.mkdir(parents=True, exist_ok=False)
                created_directories.append(local_probe_dir)
                sentinel_path.write_text("sakuramedia storage diagnostic\n", encoding="utf-8")
                mapping = cls._probe_directory_mapping(
                    client,
                    mapping,
                    qbittorrent_client_cls=qbittorrent_client_cls,
                    remote_probe_dir=remote_probe_dir,
                )
                if mapping.status == "ok":
                    hardlink = cls._probe_hardlink(
                        sentinel_path,
                        hardlink_path,
                        warnings,
                        created_directories,
                    )
        except OSError as exc:
            mapping.error = cls._storage_error(
                "filesystem_error",
                str(exc),
            )
        finally:
            warnings.extend(
                cls._cleanup_storage_probe(
                    files=[hardlink_path, sentinel_path],
                    directories=[
                        *created_directories,
                        *([] if library_diagnostic_dir_existed else [library_diagnostic_dir]),
                        *([] if local_diagnostic_dir_existed else [local_diagnostic_dir]),
                    ],
                )
            )

        healthy = mapping.status == "ok"
        return DownloadClientStorageTestResponse(
            healthy=healthy,
            checked_at=utc_now_for_db(),
            client_id=client.id,
            client_name=client.name,
            elapsed_ms=int((time.time() - start_at) * 1000),
            warnings=warnings,
            directory_mapping=mapping,
            hardlink=hardlink,
        )

    @classmethod
    def _probe_directory_mapping(
        cls,
        client: DownloadClient,
        mapping: DownloadClientStorageDirectoryMappingResult,
        *,
        qbittorrent_client_cls,
        remote_probe_dir: str,
    ) -> DownloadClientStorageDirectoryMappingResult:
        try:
            names = qbittorrent_client_cls.from_download_client(client).list_directory_names(remote_probe_dir)
        except QBittorrentClientError as exc:
            mapping.error = cls._storage_error("qbittorrent_request_error", str(exc))
            return mapping

        if cls.SENTINEL_FILENAME not in names:
            mapping.error = cls._storage_error(
                "sentinel_not_visible_to_qb",
                "qBittorrent 无法在 client_save_path 对应目录中看到后端创建的哨兵文件",
            )
            return mapping

        mapping.status = "ok"
        mapping.sentinel_visible_to_qb = True
        mapping.error = None
        return mapping

    @classmethod
    def _probe_hardlink(
        cls,
        source_path: Path,
        target_path: Path,
        warnings: list[str],
        created_directories: list[Path],
    ) -> DownloadClientStorageHardlinkResult:
        try:
            target_parent_existed = target_path.parent.exists()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_parent_existed:
                created_directories.append(target_path.parent)
            os.link(source_path, target_path)
        except OSError as exc:
            warnings.append("下载目录到媒体库不支持硬链接，导入会回退为复制")
            return DownloadClientStorageHardlinkResult(
                status="failed",
                supported=False,
                source_path=str(source_path),
                target_path=str(target_path),
                error=cls._storage_error(
                    "hardlink_not_supported",
                    str(exc),
                ),
            )
        return DownloadClientStorageHardlinkResult(
            status="ok",
            supported=True,
            source_path=str(source_path),
            target_path=str(target_path),
            error=None,
        )

    @staticmethod
    def _join_remote_path(root: str, *parts: str) -> str:
        return "/".join([root.rstrip("/"), *[part.strip("/") for part in parts if part.strip("/")]])

    @staticmethod
    def _storage_error(error_type: str, message: str) -> DownloadClientStorageTestError:
        return DownloadClientStorageTestError(type=error_type, message=message)

    @staticmethod
    def _cleanup_storage_probe(*, files: list[Path], directories: list[Path]) -> list[str]:
        warnings = []
        for path in files:
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError as exc:
                warnings.append(f"诊断临时文件清理失败: {path} ({exc})")
        seen_dirs: set[Path] = set()
        for directory in directories:
            if directory in seen_dirs:
                continue
            seen_dirs.add(directory)
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ENOTEMPTY:
                    continue
                warnings.append(f"诊断临时目录清理失败: {directory} ({exc})")
        return warnings

    @classmethod
    def update_client(
        cls,
        client_id: int,
        payload: DownloadClientUpdateRequest,
    ) -> DownloadClientResource:
        client = require_client(client_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            from src.api.exception.errors import ApiError

            raise ApiError(
                422,
                "empty_download_client_update",
                "At least one field must be provided",
            )

        if "name" in update_data:
            name = validate_non_empty(
                update_data["name"],
                "invalid_download_client_name",
                "Download client name cannot be empty",
            )
            if name != client.name:
                ensure_name_available(name, exclude_client_id=client.id)
            client.name = name

        if "base_url" in update_data:
            client.base_url = validate_base_url(update_data["base_url"])

        if "username" in update_data:
            client.username = validate_non_empty(
                update_data["username"],
                "invalid_download_client_username",
                "Download client username cannot be empty",
            )

        if "password" in update_data:
            client.password = validate_non_empty(
                update_data["password"],
                "invalid_download_client_password",
                "Download client password cannot be empty",
            )

        if "client_save_path" in update_data:
            client.client_save_path = validate_absolute_path(
                update_data["client_save_path"],
                field_name="client_save_path",
            )

        if "local_root_path" in update_data:
            client.local_root_path = validate_absolute_path(
                update_data["local_root_path"],
                field_name="local_root_path",
            )

        if "media_library_id" in update_data:
            media_library_id = validate_media_library_id(update_data["media_library_id"])
            require_media_library(media_library_id)
            client.media_library = media_library_id

        client.save()
        return DownloadClientResource.from_model(client)

    @classmethod
    def delete_client(cls, client_id: int) -> None:
        from src.api.exception.errors import ApiError

        client = require_client(client_id)
        if DownloadTask.select().where(DownloadTask.client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use",
                "Download client is still referenced by download tasks",
                {"client_id": client.id},
            )
        if Indexer.select().where(Indexer.download_client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use_by_indexers",
                "Download client is still referenced by indexers",
                {"client_id": client.id},
            )
        client.delete_instance()
