from typing import Any

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record
from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115Client,
    Cloud115CookieStatus,
    Cloud115Error,
    Cloud115QrLogin,
)
from src.model import DownloadClient, ImportJob, Media, MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.schema.playback.cloud115_libraries import (
    Cloud115BrowseResponse,
    Cloud115DirEntryResource,
    Cloud115LibraryCreateRequest,
    Cloud115LibraryReauthRequest,
)
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryResource,
    MediaLibraryUpdateRequest,
)
from src.service.cloud115 import (
    CLOUD115_DOWNLOADS_ROOT_NAME,
    CLOUD115_LIBRARY_ROOT_NAME,
    cloud115_client_for,
    find_or_create_subdir,
    map_cloud115_error,
    require_cloud115_library,
)
from src.service.playback.cloud115_qrlogin_service import Cloud115QrLoginService
from src.service.transfers.rapid_upload.query_service import (
    MediaRapidUploadQueryService,
)


class MediaLibraryService:
    @staticmethod
    def _require_library(library_id: int) -> MediaLibrary:
        return require_record(
            MediaLibrary, MediaLibrary.id == library_id,
            error_code="media_library_not_found",
            error_message="Media library not found",
            error_details={"library_id": library_id},
        )

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_media_library_name",
                "Media library name cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_local_backend_config(backend_config: dict[str, Any]) -> dict[str, Any]:
        # backend=local 时 backend_config 的合法形状 = {"root_path": <绝对路径>}。
        # 其它 backend 的形状留给对应 backend 上线时再引入。
        raw_root_path = backend_config.get("root_path")
        if not isinstance(raw_root_path, str):
            raise ApiError(
                422,
                "invalid_media_library_root_path",
                "Media library root path must be an absolute path",
            )
        normalized = raw_root_path.strip()
        if not normalized or not normalized.startswith("/"):
            raise ApiError(
                422,
                "invalid_media_library_root_path",
                "Media library root path must be an absolute path",
            )
        return {"root_path": normalized}

    @classmethod
    def _validate_backend_config(
        cls,
        backend: MediaLibraryBackend,
        backend_config: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(backend_config, dict):
            raise ApiError(
                422,
                "invalid_media_library_backend_config",
                "backend_config must be an object",
            )
        if backend is MediaLibraryBackend.LOCAL:
            return cls._validate_local_backend_config(backend_config)
        # 第 1 步只放行 local；cloud115 等其它 backend 走各自的创建 flow（例如扫码登录），
        # 不复用通用 create endpoint。
        raise ApiError(
            422,
            "unsupported_media_library_backend",
            f"Media library backend {backend.value!r} is not creatable via this endpoint",
            {"backend": backend.value},
        )

    @staticmethod
    def _ensure_name_available(name: str, exclude_library_id: int | None = None) -> None:
        query = MediaLibrary.select().where(MediaLibrary.name == name)
        if exclude_library_id is not None:
            query = query.where(MediaLibrary.id != exclude_library_id)
        if query.exists():
            raise ApiError(
                409,
                "media_library_name_conflict",
                "Media library name already exists",
                {"name": name},
            )

    @staticmethod
    def _ensure_local_root_path_available(
        root_path: str, exclude_library_id: int | None = None
    ) -> None:
        # backend_config 是 JsonTextField(TextField)，DB 层无法为 root_path 做唯一约束；
        # 用 Python 遍历所有 local 库校验（cloud115 库不参与，值域天然不冲突）。
        query = MediaLibrary.select().where(
            MediaLibrary.backend == MediaLibraryBackend.LOCAL.value
        )
        if exclude_library_id is not None:
            query = query.where(MediaLibrary.id != exclude_library_id)
        for library in query:
            existing_root = (library.backend_config or {}).get("root_path")
            if existing_root == root_path:
                raise ApiError(
                    409,
                    "media_library_root_path_conflict",
                    "Media library root path already exists",
                    {"root_path": root_path},
                )

    @staticmethod
    def _ensure_cloud_account_available(account_key: str) -> None:
        existing = MediaLibrary.get_or_none(
            MediaLibrary.backend_account_key == account_key
        )
        if existing is not None:
            raise ApiError(
                409,
                "cloud115_account_already_bound",
                "该 115 账号已经绑定到其它媒体库",
                {"library_id": existing.id},
            )

    @classmethod
    def list_libraries(cls) -> list[MediaLibraryResource]:
        libraries = list(
            MediaLibrary.select().order_by(MediaLibrary.created_at.desc(), MediaLibrary.id.desc())
        )
        return [MediaLibraryResource.from_model(library) for library in libraries]

    @classmethod
    def create_library(cls, payload: MediaLibraryCreateRequest) -> MediaLibraryResource:
        name = cls._validate_name(payload.name)
        backend_config = cls._validate_backend_config(payload.backend, payload.backend_config)
        cls._ensure_name_available(name)
        if payload.backend is MediaLibraryBackend.LOCAL:
            cls._ensure_local_root_path_available(backend_config["root_path"])
        library = MediaLibrary.create(
            name=name,
            backend=payload.backend.value,
            backend_config=backend_config,
        )
        return MediaLibraryResource.from_model(library)

    @classmethod
    def update_library(
        cls,
        library_id: int,
        payload: MediaLibraryUpdateRequest,
    ) -> MediaLibraryResource:
        library = cls._require_library(library_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_media_library_update",
                "At least one field must be provided",
            )

        if "name" in update_data:
            name = cls._validate_name(update_data["name"])
            if name != library.name:
                cls._ensure_name_available(name, exclude_library_id=library.id)
            library.name = name

        library.save()
        return MediaLibraryResource.from_model(library)

    @classmethod
    async def create_cloud115_library(
        cls,
        payload: Cloud115LibraryCreateRequest,
    ) -> MediaLibraryResource:
        """扫码 CONFIRMED 后调用：换 cookies → 校验 alive → find-or-create 库根 → 落库。

        与通用 create_library 分开是因为 cloud115 库的诞生是异步 flow（扫码 + 多步 SDK 调用），
        通用 endpoint 只放行 backend='local'。
        """
        name = cls._validate_name(payload.name)
        uid = cls._validate_cloud115_uid(payload.uid)
        app = Cloud115QrLoginService.validate_app(payload.app)
        cls._ensure_name_available(name)

        # 1) 换 cookies（uid 必须来自已 CONFIRMED 的扫码会话；否则 SDK 直接抛 AuthError）
        result = await cls._fetch_cloud115_qr_result(uid, app=app)

        # 2) 校验 cookies 真的活着（网络问题 / 服务端异常在这里现身，早失败早清清）
        try:
            async with Cloud115Client(cookies=result.cookies) as client:
                account_key = await cls._validate_cloud115_login_result(client, result)
                cls._ensure_cloud_account_available(account_key)

                # 3) find-or-create 库根 sakuramedia/ 与平级的离线下载缓冲目录
                root_cid = await cls._find_or_create_library_root(
                    client, CLOUD115_LIBRARY_ROOT_NAME
                )
                download_root_cid = await cls._find_or_create_library_root(
                    client, CLOUD115_DOWNLOADS_ROOT_NAME
                )
                # 探活和建目录响应都可能刷新 Set-Cookie；必须持久化 SDK 的最终快照。
                cookies_snapshot = client.snapshot_cookies()
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

        # 4) 落库
        try:
            library = MediaLibrary.create(
                name=name,
                backend=MediaLibraryBackend.CLOUD115.value,
                backend_account_key=account_key,
                backend_config={
                    "cookies": cookies_snapshot,
                    "root_cid": root_cid,
                    "download_root_cid": download_root_cid,
                    "app": app,
                },
            )
        except IntegrityError as exc:
            # 服务层预检提供友好错误；唯一索引兜住并发创建竞态。
            if MediaLibrary.select().where(
                MediaLibrary.backend_account_key == account_key
            ).exists():
                raise ApiError(
                    409,
                    "cloud115_account_already_bound",
                    "该 115 账号已经绑定到其它媒体库",
                ) from exc
            raise
        return MediaLibraryResource.from_model(library)

    @staticmethod
    def _validate_cloud115_uid(uid: str) -> str:
        normalized = (uid or "").strip()
        if not normalized:
            raise ApiError(422, "invalid_cloud115_uid", "uid is required")
        return normalized

    @staticmethod
    async def _fetch_cloud115_qr_result(uid: str, *, app: str):
        try:
            async with Cloud115QrLogin() as qr:
                return await qr.fetch_result(uid, app=app)
        except Cloud115AuthError as exc:
            raise ApiError(
                422,
                "cloud115_qrlogin_not_confirmed",
                "Scan the QR code and confirm on the 115 app before continuing",
                {"detail": str(exc)},
            ) from exc
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    @staticmethod
    async def _validate_cloud115_login_result(client: Cloud115Client, result) -> str:
        if not result.user_id or result.user_id != client.user_id:
            raise ApiError(
                502,
                "cloud115_user_id_mismatch",
                "115 登录结果中的用户标识不一致",
            )
        cookie_status = await client.probe_cookies_status()
        if cookie_status is Cloud115CookieStatus.EXPIRED:
            raise ApiError(
                422,
                "cloud115_cookies_invalid",
                "115 rejected the cookies immediately after login",
            )
        if cookie_status is Cloud115CookieStatus.UNAVAILABLE:
            raise ApiError(
                502,
                "cloud115_upstream_error",
                "115 登录状态暂时无法确认，请稍后重试",
            )
        return f"cloud115:{client.user_id}"

    @classmethod
    async def reauth_cloud115_library(
        cls,
        library_id: int,
        payload: Cloud115LibraryReauthRequest,
    ) -> MediaLibraryResource:
        """重新扫码更新已有库 cookies，同时保持库根与全部业务关联不变。"""
        library = cls._require_library(library_id)
        try:
            require_cloud115_library(library)
        except ValueError as exc:
            raise ApiError(
                422,
                "media_library_backend_mismatch",
                "Media library is not a cloud115 library",
                {"library_id": library_id, "detail": str(exc)},
            ) from exc

        uid = cls._validate_cloud115_uid(payload.uid)
        app = Cloud115QrLoginService.validate_app(payload.app)
        result = await cls._fetch_cloud115_qr_result(uid, app=app)
        try:
            async with Cloud115Client(cookies=result.cookies) as client:
                account_key = await cls._validate_cloud115_login_result(client, result)
                if account_key != library.backend_account_key:
                    raise ApiError(
                        409,
                        "cloud115_account_mismatch",
                        "扫码登录的 115 账号与该媒体库原账号不一致",
                        {"library_id": library.id},
                    )
                cookies_snapshot = client.snapshot_cookies()
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

        config = dict(library.backend_config or {})
        config["cookies"] = cookies_snapshot
        config["app"] = app
        library.backend_config = config
        library.updated_at = utc_now_for_db()
        library.save(only=[MediaLibrary.backend_config, MediaLibrary.updated_at])
        return MediaLibraryResource.from_model(library)

    @classmethod
    async def browse_cloud115_directory(
        cls,
        library_id: int,
        *,
        cid: str = "0",
        offset: int = 0,
        limit: int = 200,
    ) -> Cloud115BrowseResponse:
        """列 115 目录一页，供前端目录选择器选导入源。

        响应带 root_cid（库管理目录），前端据此禁选管理目录及其子树；
        服务端在导入触发时还会用 assert_cid_outside_library_root 再兜底一次。
        """
        library = cls._require_library(library_id)
        try:
            config = require_cloud115_library(library)
        except ValueError as exc:
            raise ApiError(
                422, "media_library_backend_mismatch",
                "Media library is not a cloud115 library",
                {"library_id": library_id, "detail": str(exc)},
            ) from exc
        try:
            async with cloud115_client_for(library) as client:
                entries, total = await client.list_dir(cid, offset=offset, limit=limit)
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc
        return Cloud115BrowseResponse(
            cid=cid,
            total=total,
            offset=offset,
            limit=limit,
            root_cid=config["root_cid"],
            entries=[
                Cloud115DirEntryResource(
                    entry_id=entry.entry_id,
                    name=entry.name,
                    is_dir=entry.is_dir,
                    size=entry.size,
                    is_video=entry.is_video,
                    mtime=entry.mtime,
                )
                for entry in entries
            ],
        )

    @staticmethod
    async def _find_or_create_library_root(
        client: Cloud115Client, root_name: str
    ) -> str:
        """在 115 根目录 (cid='0') 找一个叫 root_name 的子目录；没有就建。"""
        try:
            return await find_or_create_subdir(client, parent_cid="0", name=root_name)
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    @classmethod
    def delete_library(cls, library_id: int) -> None:
        library = cls._require_library(library_id)
        if (
            Media.select().where(Media.library == library.id).exists()
            or DownloadClient.select().where(DownloadClient.media_library == library.id).exists()
            or ImportJob.select().where(ImportJob.library == library.id).exists()
            or MediaRapidUploadQueryService.has_active_library(library.id)
            or MediaRapidUploadQueryService.has_library_history(library.id)
        ):
            raise ApiError(
                409,
                "media_library_in_use",
                "Media library is still referenced",
                {"library_id": library.id},
            )
        library.delete_instance()
