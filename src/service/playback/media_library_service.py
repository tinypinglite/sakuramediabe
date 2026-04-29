from src.api.exception.errors import ApiError
from src.common.service_helpers import require_record
from src.model import DownloadClient, ImportJob, Media, MediaLibrary
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
    def _validate_root_path(root_path: str) -> str:
        normalized = root_path.strip()
        if not normalized or not normalized.startswith("/"):
            raise ApiError(
                422,
                "invalid_media_library_root_path",
                "Media library root path must be an absolute path",
            )
        return normalized

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
    def _ensure_root_path_available(root_path: str, exclude_library_id: int | None = None) -> None:
        query = MediaLibrary.select().where(MediaLibrary.root_path == root_path)
        if exclude_library_id is not None:
            query = query.where(MediaLibrary.id != exclude_library_id)
        if query.exists():
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
        root_path = cls._validate_root_path(payload.root_path)
        cls._ensure_name_available(name)
        cls._ensure_root_path_available(root_path)
        library = MediaLibrary.create(name=name, root_path=root_path)
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

        if "root_path" in update_data:
            root_path = cls._validate_root_path(update_data["root_path"])
            if root_path != library.root_path:
                cls._ensure_root_path_available(root_path, exclude_library_id=library.id)
            library.root_path = root_path

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
