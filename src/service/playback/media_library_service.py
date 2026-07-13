from typing import Any

from src.api.exception.errors import ApiError
from src.common.service_helpers import require_record
from src.model import DownloadClient, ImportJob, Media, MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryResource,
    MediaLibraryUpdateRequest,
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
