from __future__ import annotations

from dataclasses import dataclass

from src.model import ImageSearchIndexState, MediaThumbnail, MoviePlotImage

INDEX_SPACE_STATE_READY = "ready"
INDEX_SPACE_STATE_REBUILD_REQUIRED = "rebuild_required"
INDEX_SPACE_STATE_UNINITIALIZED = "uninitialized"
INDEX_SPACE_STATE_UNAVAILABLE = "unavailable"
IMAGE_SEARCH_INDEX_REBUILD_REQUIRED_ERROR_CODE = "image_search_index_rebuild_required"


@dataclass(frozen=True)
class ImageSearchIndexSpaceStatus:
    state: str
    indexed_space_id: str | None
    current_space_id: str | None


class ImageSearchIndexRebuildRequiredError(RuntimeError):
    def __init__(self, status: ImageSearchIndexSpaceStatus) -> None:
        super().__init__(
            "Image search index must be rebuilt for the current embedding space"
        )
        self.status = status

    @property
    def details(self) -> dict[str, str | None]:
        return {
            "reason": (
                "space_id_changed"
                if self.status.indexed_space_id
                else "historical_space_unknown"
            ),
            "indexed_space_id": self.status.indexed_space_id,
            "current_space_id": self.status.current_space_id,
        }


class ImageSearchIndexSpaceService:
    """维护图搜索索引与当前嵌入服务之间的空间兼容性。"""

    @classmethod
    def get_status(cls, current_space_id: str | None) -> ImageSearchIndexSpaceStatus:
        state = ImageSearchIndexState.get_or_none(ImageSearchIndexState.id == 1)
        indexed_space_id = state.indexed_space_id if state is not None else None
        normalized_current_space_id = (current_space_id or "").strip() or None

        if normalized_current_space_id is None:
            return ImageSearchIndexSpaceStatus(
                state=INDEX_SPACE_STATE_UNAVAILABLE,
                indexed_space_id=indexed_space_id,
                current_space_id=None,
            )
        if indexed_space_id == normalized_current_space_id:
            return ImageSearchIndexSpaceStatus(
                state=INDEX_SPACE_STATE_READY,
                indexed_space_id=indexed_space_id,
                current_space_id=normalized_current_space_id,
            )
        if indexed_space_id is not None or cls._has_completed_index_records():
            return ImageSearchIndexSpaceStatus(
                state=INDEX_SPACE_STATE_REBUILD_REQUIRED,
                indexed_space_id=indexed_space_id,
                current_space_id=normalized_current_space_id,
            )
        return ImageSearchIndexSpaceStatus(
            state=INDEX_SPACE_STATE_UNINITIALIZED,
            indexed_space_id=None,
            current_space_id=normalized_current_space_id,
        )

    @classmethod
    def ensure_search_ready(cls, current_space_id: str) -> None:
        status = cls.get_status(current_space_id)
        if status.state == INDEX_SPACE_STATE_REBUILD_REQUIRED:
            raise ImageSearchIndexRebuildRequiredError(status)

    @classmethod
    def prepare_for_indexing(cls, current_space_id: str) -> None:
        status = cls.get_status(current_space_id)
        if status.state == INDEX_SPACE_STATE_READY:
            return
        if status.state == INDEX_SPACE_STATE_UNINITIALIZED:
            ImageSearchIndexState.create(id=1, indexed_space_id=current_space_id)
            return
        raise ImageSearchIndexRebuildRequiredError(status)

    @classmethod
    def set_indexed_space(cls, current_space_id: str) -> None:
        state = ImageSearchIndexState.get_or_none(ImageSearchIndexState.id == 1)
        if state is None:
            ImageSearchIndexState.create(id=1, indexed_space_id=current_space_id)
            return
        if state.indexed_space_id != current_space_id:
            state.indexed_space_id = current_space_id
            state.save(only=[ImageSearchIndexState.indexed_space_id])

    @staticmethod
    def _has_completed_index_records() -> bool:
        return bool(
            MediaThumbnail.select()
            .where(
                MediaThumbnail.image_search_index_status
                == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
            )
            .exists()
            or MoviePlotImage.select()
            .where(
                MoviePlotImage.image_search_index_status
                == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
            )
            .exists()
        )
