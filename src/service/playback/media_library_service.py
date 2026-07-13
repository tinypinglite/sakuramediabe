from typing import Any

from src.api.exception.errors import ApiError
from src.common.service_helpers import require_record
from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115Client,
    Cloud115Error,
    Cloud115QrLogin,
    Cloud115RateLimitedError,
)
from src.model import DownloadClient, ImportJob, Media, MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.schema.playback.cloud115_libraries import Cloud115LibraryCreateRequest
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryResource,
    MediaLibraryUpdateRequest,
)
from src.service.playback.cloud115_qrlogin_service import Cloud115QrLoginService

# 库根目录名（cloud115 库根 = 115 根下叫这个名字的目录；由系统 find-or-create）。
CLOUD115_LIBRARY_ROOT_NAME = "sakuramedia"


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

    @classmethod
    def list_libraries(cls) -> list[MediaLibraryResource]:
        libraries = list(
            MediaLibrary.select().order_by(MediaLibrary.created_at.desc(), MediaLibrary.id.desc())
        )
        return MediaLibraryResource.from_items(libraries)

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
        return MediaLibraryResource.from_attributes_model(library)

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
        return MediaLibraryResource.from_attributes_model(library)

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
        if not payload.uid:
            raise ApiError(422, "invalid_cloud115_uid", "uid is required")
        app = Cloud115QrLoginService.validate_app(payload.app)
        cls._ensure_name_available(name)

        # 1) 换 cookies（uid 必须来自已 CONFIRMED 的扫码会话；否则 SDK 直接抛 AuthError）
        try:
            async with Cloud115QrLogin() as qr:
                result = await qr.fetch_result(payload.uid, app=app)
        except Cloud115AuthError as exc:
            raise ApiError(
                422, "cloud115_qrlogin_not_confirmed",
                "Scan the QR code and confirm on the 115 app before creating the library",
                {"detail": str(exc)},
            ) from exc

        # 2) 校验 cookies 真的活着（网络问题 / 服务端异常在这里现身，早失败早清清）
        async with Cloud115Client(cookies=result.cookies) as client:
            alive = await client.check_cookies_alive()
            if not alive:
                raise ApiError(
                    422, "cloud115_cookies_invalid",
                    "115 rejected the cookies immediately after login",
                )
            # 3) find-or-create 库根 sakuramedia/
            root_cid = await cls._find_or_create_library_root(
                client, CLOUD115_LIBRARY_ROOT_NAME
            )

        # 4) 落库
        library = MediaLibrary.create(
            name=name,
            backend=MediaLibraryBackend.CLOUD115.value,
            backend_config={
                "cookies": result.cookies,
                "root_cid": root_cid,
                "app": app,
            },
        )
        return MediaLibraryResource.from_attributes_model(library)

    @staticmethod
    async def _find_or_create_library_root(
        client: Cloud115Client, root_name: str
    ) -> str:
        """在 115 根目录 (cid='0') 找一个叫 root_name 的子目录；没有就建。

        115 允许同名目录并存 → 建之前必须先翻页 list 判存在，否则会重复建。
        单账号根目录条目数一般 < 1150（服务端 list_dir 上限），一次分页足够。
        """
        try:
            offset = 0
            while True:
                entries, total = await client.list_dir(
                    "0", offset=offset, limit=1150
                )
                for entry in entries:
                    if entry.is_dir and entry.name == root_name:
                        return entry.entry_id
                offset += len(entries)
                if not entries or offset >= total:
                    break
            return await client.mkdir("0", root_name)
        except Cloud115RateLimitedError as exc:
            raise ApiError(
                429, "cloud115_rate_limited",
                "115 is rate limiting; try again shortly",
                {"detail": str(exc)},
            ) from exc
        except Cloud115Error as exc:
            raise ApiError(
                502, "cloud115_upstream_error",
                "115 upstream call failed while preparing library root",
                {"detail": str(exc)},
            ) from exc

    @classmethod
    def delete_library(cls, library_id: int) -> None:
        library = cls._require_library(library_id)
        if (
            Media.select().where(Media.library == library.id).exists()
            or DownloadClient.select().where(DownloadClient.media_library == library.id).exists()
            or ImportJob.select().where(ImportJob.library == library.id).exists()
        ):
            raise ApiError(
                409,
                "media_library_in_use",
                "Media library is still referenced",
                {"library_id": library.id},
            )
        library.delete_instance()
