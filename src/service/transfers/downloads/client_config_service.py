import asyncio
import errno
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import Cloud115CookieStatus
from src.model import DownloadClient, DownloadTask, IndexerDownloadClient
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadClientCreateRequest,
    DownloadClientProbeStorageTestRequest,
    DownloadClientProbeTestRequest,
    DownloadClientResource,
    DownloadClientStorageDirectoryMappingResult,
    DownloadClientStorageHardlinkResult,
    DownloadClientStorageTestError,
    DownloadClientStorageTestResponse,
    DownloadClientTestError,
    DownloadClientTestResponse,
    DownloadClientUpdateRequest,
)
from src.service.cloud115 import Cloud115KeepaliveService
from src.service.transfers.shared.common import (
    ensure_name_available,
    require_client,
    require_cloud115_media_library,
    require_local_media_library,
    validate_absolute_path,
    validate_base_url,
    validate_download_client_kind,
    validate_media_library_id,
    validate_non_empty,
)
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)


@dataclass
class _MediaLibraryView:
    backend_config: dict


@dataclass
class _DownloadClientView:
    """内存态的 client 视图,供 probe 端点复用 test_client / test_storage 核心逻辑。

    字段名与 `DownloadClient` peewee 模型对齐,`QBittorrentClient.from_download_client`
    与 `_probe_directory_mapping` 等函数通过属性访问即可直接消费。
    """

    id: int
    name: str
    base_url: str
    username: str
    password: str
    client_save_path: str
    local_root_path: str
    media_library: _MediaLibraryView


class DownloadClientService:
    DIAGNOSTIC_DIR_NAME = ".sakuramedia-diagnostics"
    SENTINEL_FILENAME = "sentinel.txt"
    HARDLINK_FILENAME = "sentinel.link"

    @classmethod
    def list_clients(cls) -> list[DownloadClientResource]:
        clients = list(
            DownloadClient.select().order_by(
                DownloadClient.created_at.desc(),
                DownloadClient.id.desc(),
            )
        )
        return DownloadClientResource.from_models(clients)

    @classmethod
    def create_client(cls, payload: DownloadClientCreateRequest) -> DownloadClientResource:
        kind = validate_download_client_kind(payload.kind)
        name = validate_non_empty(
            payload.name,
            "invalid_download_client_name",
            "Download client name cannot be empty",
        )
        media_library_id = validate_media_library_id(payload.media_library_id)
        ensure_name_available(name)

        if kind == DownloadClientKind.CLOUD115.value:
            # cloud115 入口不带连接字段：凭据与落地目录都挂在 cloud115 媒体库配置上。
            require_cloud115_media_library(media_library_id)
            cls._ensure_cloud115_library_available(media_library_id)
            try:
                client = DownloadClient.create(
                    name=name,
                    kind=kind,
                    media_library=media_library_id,
                )
            except IntegrityError:
                # 并发创建由数据库部分唯一索引兜底，并映射为稳定的领域错误。
                cls._ensure_cloud115_library_available(media_library_id)
                raise
            return DownloadClientResource.from_model(client)

        username = validate_non_empty(
            payload.username or "",
            "invalid_download_client_username",
            "Download client username cannot be empty",
        )
        password = validate_non_empty(
            payload.password or "",
            "invalid_download_client_password",
            "Download client password cannot be empty",
        )
        require_local_media_library(media_library_id)

        client = DownloadClient.create(
            name=name,
            kind=kind,
            base_url=validate_base_url(payload.base_url or ""),
            username=username,
            password=password,
            client_save_path=validate_absolute_path(
                payload.client_save_path or "",
                field_name="client_save_path",
            ),
            local_root_path=validate_absolute_path(
                payload.local_root_path or "",
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
        if client.kind == DownloadClientKind.CLOUD115.value:
            return cls._run_cloud115_probe(client)
        return cls._run_connectivity_probe(
            client,
            qbittorrent_client_cls=qbittorrent_client_cls,
        )

    @classmethod
    def _run_cloud115_probe(cls, client) -> DownloadClientTestResponse:
        """cloud115 入口的连通性 = 所挂媒体库 cookies 是否存活（探活会顺带回写快照）。"""
        start_at = time.time()
        cookie_status = asyncio.run(
            Cloud115KeepaliveService.probe_library_cookies_status(client.media_library)
        )
        if cookie_status is Cloud115CookieStatus.ALIVE:
            return cls._build_test_response(client, start_at=start_at, healthy=True)
        error_type = (
            "cloud115_cookies_invalid"
            if cookie_status is Cloud115CookieStatus.EXPIRED
            else "cloud115_upstream_error"
        )
        error_message = (
            "115 cookies 已失效，请重新扫码登录"
            if cookie_status is Cloud115CookieStatus.EXPIRED
            else "115 上游暂时不可用，请稍后再试"
        )
        return cls._build_test_response(
            client,
            start_at=start_at,
            healthy=False,
            error=DownloadClientTestError(type=error_type, message=error_message),
        )

    @classmethod
    def _run_connectivity_probe(
        cls,
        client,
        *,
        qbittorrent_client_cls,
    ) -> DownloadClientTestResponse:
        """核心连通性探测,同时服务于已落库 client 与 probe 端点的内存态视图。"""
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
        client,
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
        if client.kind != DownloadClientKind.QBITTORRENT.value:
            # 存储探测（目录映射 + 硬链接）是 qb 专属概念；cloud115 无本地磁盘语义。
            raise ApiError(
                422,
                "download_client_kind_mismatch",
                "Storage test is only supported for qbittorrent clients",
                {"client_id": client.id, "kind": client.kind},
            )
        require_local_media_library(client.media_library_id)
        return cls._run_storage_probe(
            client,
            qbittorrent_client_cls=qbittorrent_client_cls,
            probe_id=probe_id,
        )

    @classmethod
    def _run_storage_probe(
        cls,
        client,
        *,
        qbittorrent_client_cls,
        probe_id: str | None = None,
    ) -> DownloadClientStorageTestResponse:
        """核心存储探测,client 可为 peewee 模型或 `_DownloadClientView`。"""
        start_at = time.time()
        probe_id = probe_id or uuid.uuid4().hex
        local_root = Path(client.local_root_path).expanduser()
        library_root = Path(client.media_library.backend_config["root_path"]).expanduser()
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

        # 只有本次探测自己 mkdir 出来的子树才会在 finally 里被 rmtree,
        # 避免撞上事先存在的同名目录时误删用户数据。
        owned_probe_dirs: list[Path] = []
        owned_probe_files: list[Path] = []
        try:
            if not local_root.exists() or not local_root.is_dir():
                mapping.error = cls._storage_error(
                    "local_root_path_not_accessible",
                    "local_root_path 不存在或不是目录",
                )
            else:
                # 哨兵文件放在专用诊断目录内，避免直接污染下载根目录。
                local_probe_dir.mkdir(parents=True, exist_ok=False)
                owned_probe_dirs.append(local_probe_dir)
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
                        owned_probe_dirs=owned_probe_dirs,
                        owned_probe_files=owned_probe_files,
                    )
        except OSError as exc:
            mapping.error = cls._storage_error(
                "filesystem_error",
                str(exc),
            )
        finally:
            warnings.extend(
                cls._cleanup_storage_probe(
                    probe_files=owned_probe_files,
                    probe_dirs=owned_probe_dirs,
                    diagnostic_dirs=[
                        *([] if local_diagnostic_dir_existed else [local_diagnostic_dir]),
                        *([] if library_diagnostic_dir_existed else [library_diagnostic_dir]),
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
        *,
        owned_probe_dirs: list[Path],
        owned_probe_files: list[Path],
    ) -> DownloadClientStorageHardlinkResult:
        try:
            # 只把本次真正新建的 library_probe_dir 登记进清理列表；已存在时保持原样，防止误删。
            target_parent_existed = target_path.parent.exists()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_parent_existed:
                owned_probe_dirs.append(target_path.parent)
            os.link(source_path, target_path)
            # 目标目录预先存在时也只清理本次创建的 hardlink 文件，避免留下诊断痕迹。
            owned_probe_files.append(target_path)
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

    @classmethod
    def probe_test(
        cls,
        payload: DownloadClientProbeTestRequest,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadClientTestResponse:
        """连通性预检:接受表单 payload,不落库。"""
        client_view = cls._build_probe_view_for_connectivity(payload)
        return cls._run_connectivity_probe(
            client_view,
            qbittorrent_client_cls=qbittorrent_client_cls,
        )

    @classmethod
    def probe_storage_test(
        cls,
        payload: DownloadClientProbeStorageTestRequest,
        *,
        qbittorrent_client_cls=QBittorrentClient,
        probe_id: str | None = None,
    ) -> DownloadClientStorageTestResponse:
        """目录映射 + 硬链接预检:接受表单 payload,不落库。"""
        client_view = cls._build_probe_view_for_storage(payload)
        return cls._run_storage_probe(
            client_view,
            qbittorrent_client_cls=qbittorrent_client_cls,
            probe_id=probe_id,
        )

    @classmethod
    def _build_probe_view_for_connectivity(
        cls,
        payload: DownloadClientProbeTestRequest,
    ) -> "_DownloadClientView":
        base_url = validate_base_url(payload.base_url)
        username = validate_non_empty(
            payload.username,
            "invalid_download_client_username",
            "Download client username cannot be empty",
        )
        password, existing = cls._resolve_probe_password(
            password=payload.password,
            client_id=payload.client_id,
        )
        return _DownloadClientView(
            id=existing.id if existing is not None else 0,
            name=existing.name if existing is not None else "",
            base_url=base_url,
            username=username,
            password=password,
            client_save_path=existing.client_save_path if existing is not None else "",
            local_root_path=existing.local_root_path if existing is not None else "",
            media_library=_MediaLibraryView(
                backend_config=(
                    dict(existing.media_library.backend_config)
                    if existing is not None
                    else {"root_path": ""}
                ),
            ),
        )

    @classmethod
    def _build_probe_view_for_storage(
        cls,
        payload: DownloadClientProbeStorageTestRequest,
    ) -> "_DownloadClientView":
        base_url = validate_base_url(payload.base_url)
        username = validate_non_empty(
            payload.username,
            "invalid_download_client_username",
            "Download client username cannot be empty",
        )
        client_save_path = validate_absolute_path(
            payload.client_save_path,
            field_name="client_save_path",
        )
        local_root_path = validate_absolute_path(
            payload.local_root_path,
            field_name="local_root_path",
        )
        media_library_id = validate_media_library_id(payload.media_library_id)
        library = require_local_media_library(media_library_id)
        password, existing = cls._resolve_probe_password(
            password=payload.password,
            client_id=payload.client_id,
        )
        return _DownloadClientView(
            id=existing.id if existing is not None else 0,
            name=existing.name if existing is not None else "",
            base_url=base_url,
            username=username,
            password=password,
            client_save_path=client_save_path,
            local_root_path=local_root_path,
            media_library=_MediaLibraryView(backend_config=dict(library.backend_config)),
        )

    @staticmethod
    def _resolve_probe_password(
        *,
        password: str | None,
        client_id: int | None,
    ) -> tuple[str, DownloadClient | None]:
        """合并密码:留空则要求 client_id,从 DB 取原密码;否则密码必填。"""
        normalized = (password or "").strip()
        if normalized:
            existing = require_client(client_id) if client_id else None
            return normalized, existing
        if not client_id:
            raise ApiError(
                422,
                "invalid_download_client_password",
                "Download client password cannot be empty",
            )
        existing = require_client(client_id)
        return existing.password, existing

    @staticmethod
    def _join_remote_path(root: str, *parts: str) -> str:
        return "/".join([root.rstrip("/"), *[part.strip("/") for part in parts if part.strip("/")]])

    @staticmethod
    def _storage_error(error_type: str, message: str) -> DownloadClientStorageTestError:
        return DownloadClientStorageTestError(type=error_type, message=message)

    @staticmethod
    def _cleanup_storage_probe(
        *,
        probe_files: list[Path],
        probe_dirs: list[Path],
        diagnostic_dirs: list[Path],
    ) -> list[str]:
        warnings: list[str] = []
        for path in probe_files:
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError as exc:
                warnings.append(f"诊断临时文件清理失败: {path} ({exc})")
        # probe_dirs 是本次 probe 独占的 uuid 子目录，递归删除；不存在则忽略。
        for path in probe_dirs:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append(f"诊断临时目录清理失败: {path} ({exc})")
        # 诊断根目录仅在本次新建时才尝试删除，且只删空目录，避免踩踏并发的其他 probe。
        seen: set[Path] = set()
        for directory in diagnostic_dirs:
            if directory in seen:
                continue
            seen.add(directory)
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ENOTEMPTY:
                    continue
                warnings.append(f"诊断临时目录清理失败: {directory} ({exc})")
        return warnings

    # cloud115 入口只允许改名；换账号或换库必须解绑后删除重建。
    CLOUD115_UPDATABLE_FIELDS = {"name"}

    @staticmethod
    def _ensure_cloud115_library_available(media_library_id: int) -> None:
        existing = DownloadClient.get_or_none(
            (DownloadClient.kind == DownloadClientKind.CLOUD115.value)
            & (DownloadClient.media_library == media_library_id)
        )
        if existing is not None:
            raise ApiError(
                409,
                "cloud115_download_client_already_exists",
                "同一 115 媒体库只能创建一个离线下载入口",
                {"media_library_id": media_library_id, "client_id": existing.id},
            )

    @classmethod
    def update_client(
        cls,
        client_id: int,
        payload: DownloadClientUpdateRequest,
    ) -> DownloadClientResource:
        client = require_client(client_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_download_client_update",
                "At least one field must be provided",
            )

        if client.kind == DownloadClientKind.CLOUD115.value:
            if "media_library_id" in update_data:
                raise ApiError(
                    409,
                    "cloud115_download_client_library_immutable",
                    "115 离线下载入口不允许换绑媒体库，请解绑后删除重建",
                    {"client_id": client.id},
                )
            invalid_fields = sorted(set(update_data) - cls.CLOUD115_UPDATABLE_FIELDS)
            if invalid_fields:
                raise ApiError(
                    422,
                    "invalid_download_client_update_fields",
                    "cloud115 download client only supports updating name",
                    {"fields": invalid_fields},
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
            if client.kind == DownloadClientKind.CLOUD115.value:
                require_cloud115_media_library(media_library_id)
            else:
                require_local_media_library(media_library_id)
            client.media_library = media_library_id

        client.save()
        return DownloadClientResource.from_model(client)

    @classmethod
    def delete_client(cls, client_id: int) -> None:
        client = require_client(client_id)
        if DownloadTask.select().where(DownloadTask.client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use",
                "Download client is still referenced by download tasks",
                {"client_id": client.id},
            )
        if IndexerDownloadClient.select().where(IndexerDownloadClient.download_client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use_by_indexers",
                "Download client is still referenced by indexers",
                {"client_id": client.id},
            )
        client.delete_instance()
