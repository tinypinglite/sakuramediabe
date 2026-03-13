import base64
import json
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Sequence

from loguru import logger
from peewee import JOIN

from src.config.config import settings
from src.model import Image, ImageSearchSession, Media, MediaThumbnail, Movie
from src.schema.catalog.actors import ImageResource
from src.schema.discovery import (
    ImageSearchResultItemResource,
    ImageSearchSessionPageResource,
    ImageSearchSessionResource,
)
from src.service.discovery.joytag_openvino_embedder import get_joytag_openvino_embedder
from src.service.discovery.lancedb_thumbnail_store import (
    LanceDbThumbnailStore,
    ThumbnailVectorSearchHit,
    get_lancedb_thumbnail_store,
)


class ImageSearchService:
    CURSOR_VERSION = 1

    def __init__(
        self,
        store: LanceDbThumbnailStore | None = None,
        embedder=None,
    ) -> None:
        self.store = store or get_lancedb_thumbnail_store()
        self.embedder = embedder or get_joytag_openvino_embedder()

    @staticmethod
    def _now() -> datetime:
        return datetime.utcnow()

    @staticmethod
    def _normalize_ids(ids: Sequence[int] | None) -> list[int] | None:
        if not ids:
            return None
        return [int(item) for item in dict.fromkeys(ids)]

    @classmethod
    def _encode_cursor(cls, offset: int) -> str:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        raw = json.dumps({"v": cls.CURSOR_VERSION, "offset": int(offset)}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")

    @classmethod
    def _decode_cursor(cls, cursor: str) -> int:
        if not cursor:
            raise ValueError("cursor is required")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid cursor") from exc
        if not isinstance(payload, dict) or int(payload.get("v", -1)) != cls.CURSOR_VERSION:
            raise ValueError("invalid cursor")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid cursor")
        return offset

    @staticmethod
    def _normalize_page_size(page_size: int | None) -> int:
        if page_size is None:
            return settings.image_search.default_page_size
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if page_size > settings.image_search.max_page_size:
            raise ValueError(f"page_size must be <= {settings.image_search.max_page_size}")
        return page_size

    @classmethod
    def _purge_expired_sessions(cls) -> None:
        ImageSearchSession.delete().where(ImageSearchSession.expires_at <= cls._now()).execute()

    @classmethod
    def _get_session_model(cls, session_id: str) -> ImageSearchSession:
        cls._purge_expired_sessions()
        session = ImageSearchSession.get_or_none(ImageSearchSession.session_id == session_id)
        if session is None:
            raise LookupError("image search session not found or expired")
        return session

    @staticmethod
    def _session_resource(session: ImageSearchSession) -> ImageSearchSessionResource:
        return ImageSearchSessionResource(
            session_id=session.session_id,
            status=session.status,
            page_size=session.page_size,
            next_cursor=session.next_cursor,
            expires_at=session.expires_at,
        )

    def get_session(self, session_id: str) -> ImageSearchSessionResource:
        return self._session_resource(self._get_session_model(session_id))

    def create_session_and_first_page(
        self,
        image_bytes: bytes,
        page_size: int | None = None,
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
        score_threshold: float | None = None,
    ) -> ImageSearchSessionPageResource:
        if not image_bytes:
            raise ValueError("image file is empty")
        normalized_page_size = self._normalize_page_size(page_size)
        normalized_movie_ids = self._normalize_ids(movie_ids)
        normalized_exclude_ids = self._normalize_ids(exclude_movie_ids)
        if score_threshold is not None and not 0 <= float(score_threshold) <= 1:
            raise ValueError("score_threshold must be between 0 and 1")

        self._purge_expired_sessions()
        inference = self.embedder.infer_image_bytes(image_bytes)
        now = self._now()
        expires_at = now + timedelta(seconds=settings.image_search.session_ttl_seconds)
        session = ImageSearchSession.create(
            session_id=uuid.uuid4().hex,
            status="ready",
            page_size=normalized_page_size,
            query_vector=[float(item) for item in inference.vector],
            movie_ids=normalized_movie_ids,
            exclude_movie_ids=normalized_exclude_ids,
            score_threshold=float(score_threshold) if score_threshold is not None else None,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        return self._search_page(session, offset=0)

    def list_results(
        self,
        session_id: str,
        cursor: str | None = None,
    ) -> ImageSearchSessionPageResource:
        session = self._get_session_model(session_id)
        offset = 0 if cursor is None else self._decode_cursor(cursor)
        return self._search_page(session, offset=offset)

    def _search_page(
        self,
        session: ImageSearchSession,
        offset: int,
    ) -> ImageSearchSessionPageResource:
        page_items: list[ImageSearchResultItemResource] = []
        next_cursor = None
        batch_size = max(session.page_size, settings.image_search.search_scan_batch_size)
        raw_offset = int(offset)

        while len(page_items) < session.page_size:
            hits = self.store.search(
                query_vector=session.query_vector or [],
                limit=batch_size,
                offset=raw_offset,
                movie_ids=session.movie_ids,
                exclude_movie_ids=session.exclude_movie_ids,
            )
            if not hits:
                break

            thumbnails_by_id = self._get_thumbnails_by_ids([item.thumbnail_id for item in hits])
            for index, hit in enumerate(hits, start=1):
                raw_offset += 1
                item = self._build_item(hit, thumbnails_by_id.get(hit.thumbnail_id), session.score_threshold)
                if item is None:
                    continue
                page_items.append(item)
                if len(page_items) >= session.page_size:
                    has_more = index < len(hits)
                    if not has_more and len(hits) >= batch_size:
                        has_more = bool(
                            self.store.search(
                                query_vector=session.query_vector or [],
                                limit=1,
                                offset=raw_offset,
                                movie_ids=session.movie_ids,
                                exclude_movie_ids=session.exclude_movie_ids,
                            )
                        )
                    if has_more:
                        next_cursor = self._encode_cursor(raw_offset)
                    break

            if len(page_items) >= session.page_size:
                break
            if len(hits) < batch_size:
                break

        session.next_cursor = next_cursor
        session.updated_at = self._now()
        session.save(only=[ImageSearchSession.next_cursor, ImageSearchSession.updated_at])
        return ImageSearchSessionPageResource(
            session_id=session.session_id,
            status=session.status,
            page_size=session.page_size,
            next_cursor=next_cursor,
            expires_at=session.expires_at,
            items=page_items,
        )

    @staticmethod
    def _build_item(
        hit: ThumbnailVectorSearchHit,
        thumbnail: MediaThumbnail | None,
        score_threshold: float | None,
    ) -> ImageSearchResultItemResource | None:
        if score_threshold is not None and hit.score < float(score_threshold):
            return None
        if thumbnail is None:
            logger.warning("Image search hit thumbnail not found thumbnail_id={}", hit.thumbnail_id)
            return None
        media = thumbnail.media
        movie = media.movie
        if not media.valid:
            return None
        return ImageSearchResultItemResource(
            thumbnail_id=thumbnail.id,
            media_id=media.id,
            movie_id=movie.id,
            movie_number=movie.movie_number,
            offset_seconds=thumbnail.offset,
            score=hit.score,
            image=ImageResource.from_attributes_model(thumbnail.image),
        )

    @staticmethod
    def _get_thumbnails_by_ids(thumbnail_ids: Sequence[int]) -> dict[int, MediaThumbnail]:
        if not thumbnail_ids:
            return {}
        unique_ids = [int(item) for item in dict.fromkeys(thumbnail_ids)]
        query = (
            MediaThumbnail.select(MediaThumbnail, Image, Media, Movie)
            .join(Image)
            .switch(MediaThumbnail)
            .join(Media)
            .join(Movie, JOIN.INNER, on=(Media.movie == Movie.movie_number))
            .where(MediaThumbnail.id.in_(unique_ids))
        )
        return {int(thumbnail.id): thumbnail for thumbnail in query}


@lru_cache(maxsize=1)
def get_image_search_service() -> ImageSearchService:
    return ImageSearchService()
