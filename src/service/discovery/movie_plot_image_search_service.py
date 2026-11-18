import base64
import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache

from loguru import logger
from peewee import JOIN

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Image, ImageSearchSession, Movie, MoviePlotImage
from src.schema.catalog.actors import ImageResource
from src.schema.discovery.image_search import (
    MoviePlotImageSearchResultItemResource,
    MoviePlotImageSearchSessionPageResource,
)

from .embedding_client import EmbeddingClientError, get_embedding_client
from .image_search_index_space_service import (
    IMAGE_SEARCH_INDEX_REBUILD_REQUIRED_ERROR_CODE,
    ImageSearchIndexRebuildRequiredError,
    ImageSearchIndexSpaceService,
)
from .qdrant_plot_image_store import (
    PlotImageVectorSearchHit,
    QdrantPlotImageStore,
    get_qdrant_plot_image_store,
)


class MoviePlotImageSearchService:
    CURSOR_VERSION = 1

    def __init__(
        self, store: QdrantPlotImageStore | None = None, embedder=None
    ) -> None:
        self.store = store or get_qdrant_plot_image_store()
        self.embedder = embedder or get_embedding_client()

    @staticmethod
    def _normalize_ids(ids: Sequence[int] | None) -> list[int] | None:
        return [int(item) for item in dict.fromkeys(ids)] if ids else None

    @classmethod
    def _encode_cursor(cls, offset: int) -> str:
        raw = json.dumps(
            {"v": cls.CURSOR_VERSION, "offset": offset}, separators=(",", ":")
        )
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def _decode_cursor(cls, cursor: str) -> int:
        try:
            payload = json.loads(
                base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
            )
            offset = (
                payload.get("offset")
                if int(payload.get("v", -1)) == cls.CURSOR_VERSION
                else None
            )
        except Exception as exc:
            raise ValueError("invalid cursor") from exc
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid cursor")
        return offset

    @staticmethod
    def _page_size(page_size: int | None) -> int:
        value = (
            settings.image_search.default_page_size if page_size is None else page_size
        )
        if not 0 < value <= settings.image_search.max_page_size:
            raise ValueError(
                f"page_size must be between 1 and {settings.image_search.max_page_size}"
            )
        return value

    @classmethod
    def _purge_expired_sessions(cls) -> None:
        ImageSearchSession.delete().where(
            ImageSearchSession.expires_at <= utc_now_for_db()
        ).execute()

    def _ensure_searchable_index(self) -> None:
        try:
            space = self.embedder.describe()
        except EmbeddingClientError as exc:
            raise ApiError(exc.status_code, exc.error_code, exc.message) from exc
        try:
            ImageSearchIndexSpaceService.ensure_search_ready(space.space_id)
        except ImageSearchIndexRebuildRequiredError as exc:
            raise ApiError(
                409,
                IMAGE_SEARCH_INDEX_REBUILD_REQUIRED_ERROR_CODE,
                "Image search index must be rebuilt for the current embedding space",
                exc.details,
            ) from exc

    def create_session_and_first_page(
        self,
        image_bytes: bytes,
        page_size: int | None = None,
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
        score_threshold: float | None = None,
    ) -> MoviePlotImageSearchSessionPageResource:
        if not image_bytes:
            raise ValueError("image file is empty")
        if score_threshold is not None and not 0 <= float(score_threshold) <= 1:
            raise ValueError("score_threshold must be between 0 and 1")
        self._ensure_searchable_index()
        try:
            vector = self.embedder.embed_images([image_bytes])[0]
        except EmbeddingClientError as exc:
            raise ApiError(exc.status_code, exc.error_code, exc.message) from exc
        self._purge_expired_sessions()
        now = utc_now_for_db()
        session = ImageSearchSession.create(
            session_id=uuid.uuid4().hex,
            status="ready",
            page_size=self._page_size(page_size),
            query_vector=[float(item) for item in vector],
            movie_ids=self._normalize_ids(movie_ids),
            exclude_movie_ids=self._normalize_ids(exclude_movie_ids),
            score_threshold=float(score_threshold)
            if score_threshold is not None
            else None,
            expires_at=now
            + timedelta(seconds=settings.image_search.session_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        return self._search_page(session, 0)

    def create_text_session_and_first_page(
        self,
        text: str,
        page_size: int | None = None,
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
        score_threshold: float | None = None,
    ) -> MoviePlotImageSearchSessionPageResource:
        if score_threshold is not None and not 0 <= float(score_threshold) <= 1:
            raise ValueError("score_threshold must be between 0 and 1")
        self._ensure_searchable_index()
        try:
            vector = self.embedder.embed_texts([text])[0]
        except EmbeddingClientError as exc:
            raise ApiError(exc.status_code, exc.error_code, exc.message) from exc
        self._purge_expired_sessions()
        now = utc_now_for_db()
        session = ImageSearchSession.create(
            session_id=uuid.uuid4().hex,
            status="ready",
            page_size=self._page_size(page_size),
            query_vector=[float(item) for item in vector],
            movie_ids=self._normalize_ids(movie_ids),
            exclude_movie_ids=self._normalize_ids(exclude_movie_ids),
            score_threshold=float(score_threshold) if score_threshold is not None else None,
            expires_at=now + timedelta(seconds=settings.image_search.session_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        return self._search_page(session, 0)

    def list_results(
        self, session_id: str, cursor: str | None = None
    ) -> MoviePlotImageSearchSessionPageResource:
        self._ensure_searchable_index()
        self._purge_expired_sessions()
        session = ImageSearchSession.get_or_none(
            ImageSearchSession.session_id == session_id
        )
        if session is None:
            raise LookupError("plot image search session not found or expired")
        return self._search_page(
            session, 0 if cursor is None else self._decode_cursor(cursor)
        )

    def _search_page(
        self, session: ImageSearchSession, offset: int
    ) -> MoviePlotImageSearchSessionPageResource:
        items: list[MoviePlotImageSearchResultItemResource] = []
        next_cursor = None
        batch_size = max(
            session.page_size, settings.image_search.search_scan_batch_size
        )
        raw_offset = offset
        while len(items) < session.page_size:
            hits = self.store.search(
                session.query_vector or [],
                batch_size,
                raw_offset,
                session.movie_ids,
                session.exclude_movie_ids,
            )
            if not hits:
                break
            links = self._get_links([item.plot_image_id for item in hits])
            for index, hit in enumerate(hits, start=1):
                raw_offset += 1
                item = self._build_item(
                    hit, links.get(hit.plot_image_id), session.score_threshold
                )
                if item is not None:
                    items.append(item)
                if len(items) == session.page_size:
                    if index < len(hits) or (
                        len(hits) == batch_size
                        and self.store.search(
                            session.query_vector or [],
                            1,
                            raw_offset,
                            session.movie_ids,
                            session.exclude_movie_ids,
                        )
                    ):
                        next_cursor = self._encode_cursor(raw_offset)
                    break
            if len(items) == session.page_size or len(hits) < batch_size:
                break
        session.next_cursor = next_cursor
        session.updated_at = utc_now_for_db()
        session.save(
            only=[
                ImageSearchSession.next_cursor,
                ImageSearchSession.updated_at,
            ]
        )
        return MoviePlotImageSearchSessionPageResource(
            session_id=session.session_id,
            status=session.status,
            page_size=session.page_size,
            next_cursor=next_cursor,
            expires_at=session.expires_at,
            items=items,
        )

    @staticmethod
    def _get_links(plot_image_ids: Sequence[int]) -> dict[int, MoviePlotImage]:
        if not plot_image_ids:
            return {}
        query = (
            MoviePlotImage.select(MoviePlotImage, Image, Movie)
            .join(Image)
            .switch(MoviePlotImage)
            .join(Movie, JOIN.INNER)
            .where(
                MoviePlotImage.id.in_(
                    [int(item) for item in dict.fromkeys(plot_image_ids)]
                ),
                Movie.is_blacklisted == False,
            )
        )
        return {link.id: link for link in query}

    @staticmethod
    def _build_item(
        hit: PlotImageVectorSearchHit,
        link: MoviePlotImage | None,
        score_threshold: float | None,
    ) -> MoviePlotImageSearchResultItemResource | None:
        if score_threshold is not None and hit.score < score_threshold:
            return None
        if link is None:
            logger.warning(
                "Plot image search hit not found plot_image_id={}", hit.plot_image_id
            )
            return None
        return MoviePlotImageSearchResultItemResource(
            plot_image_id=link.id,
            movie_id=link.movie_id,
            movie_number=link.movie.movie_number,
            score=hit.score,
            image=ImageResource.from_attributes_model(link.image),
        )


@lru_cache(maxsize=1)
def get_movie_plot_image_search_service() -> MoviePlotImageSearchService:
    return MoviePlotImageSearchService()
