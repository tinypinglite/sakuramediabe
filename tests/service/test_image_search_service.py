from datetime import datetime, timedelta

import pytest

from src.model import Image, ImageSearchSession, Media, MediaLibrary, MediaThumbnail, Movie, MovieSeries
from src.service.discovery.image_search_service import ImageSearchService
from src.service.discovery.lancedb_thumbnail_store import ThumbnailVectorSearchHit


class _DummyEmbedder:
    def infer_image_bytes(self, _image_bytes: bytes):
        class _Result:
            vector = [0.1, 0.2, 0.3]

        return _Result()


class _DummyStore:
    def __init__(self, hits_by_offset):
        self.hits_by_offset = hits_by_offset
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits_by_offset.get(kwargs["offset"], [])


@pytest.fixture()
def image_search_tables(test_db):
    models = [Image, MovieSeries, Movie, MediaLibrary, Media, MediaThumbnail, ImageSearchSession]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield
    test_db.drop_tables(list(reversed(models)))


def _create_thumbnail(movie_number: str, offset: int, *, valid: bool = True):
    movie = Movie.create(javdb_id=f"javdb-{movie_number}", movie_number=movie_number, title=movie_number)
    media = Media.create(movie=movie, path=f"/library/{movie_number}.mp4", valid=valid)
    image = Image.create(
        origin=f"movies/{movie_number}/media/thumb-{offset}.webp",
        small=f"movies/{movie_number}/media/thumb-{offset}.webp",
        medium=f"movies/{movie_number}/media/thumb-{offset}.webp",
        large=f"movies/{movie_number}/media/thumb-{offset}.webp",
    )
    return MediaThumbnail.create(media=media, image=image, offset=offset)


def _hit(thumbnail_id: int, media_id: int, movie_id: int, offset_seconds: int, score: float):
    return ThumbnailVectorSearchHit(
        thumbnail_id=thumbnail_id,
        media_id=media_id,
        movie_id=movie_id,
        offset_seconds=offset_seconds,
        score=score,
    )


def test_create_session_returns_first_page_and_next_cursor(image_search_tables, monkeypatch):
    first = _create_thumbnail("ABC-001", 10)
    second = _create_thumbnail("ABC-002", 20)
    third = _create_thumbnail("ABC-003", 30)
    store = _DummyStore(
        {
            0: [
                _hit(first.id, first.media_id, first.media.movie.id, 10, 0.9),
                _hit(second.id, second.media_id, second.media.movie.id, 20, 0.8),
            ],
            2: [
                _hit(third.id, third.media_id, third.media.movie.id, 30, 0.7),
            ],
        }
    )
    monkeypatch.setattr("src.service.discovery.image_search_service.settings.image_search.search_scan_batch_size", 2)
    monkeypatch.setattr(ImageSearchService, "_now", staticmethod(lambda: datetime(2026, 3, 13, 10, 0, 0)))
    service = ImageSearchService(store=store, embedder=_DummyEmbedder())

    first_page = service.create_session_and_first_page(image_bytes=b"img", page_size=2)
    second_page = service.list_results(first_page.session_id, cursor=first_page.next_cursor)
    session = ImageSearchSession.get(ImageSearchSession.session_id == first_page.session_id)

    assert [item.thumbnail_id for item in first_page.items] == [first.id, second.id]
    assert first_page.next_cursor is not None
    assert [item.thumbnail_id for item in second_page.items] == [third.id]
    assert second_page.next_cursor is None
    assert session.query_vector == [0.1, 0.2, 0.3]
    assert session.movie_ids is None


def test_list_results_rejects_invalid_cursor(image_search_tables, monkeypatch):
    monkeypatch.setattr(ImageSearchService, "_now", staticmethod(lambda: datetime(2026, 3, 13, 9, 0, 0)))
    service = ImageSearchService(store=_DummyStore({}), embedder=_DummyEmbedder())
    ImageSearchSession.create(
        session_id="session-1",
        query_vector=[0.1],
        expires_at=datetime(2026, 3, 13, 10, 0, 0),
    )

    with pytest.raises(ValueError):
        service.list_results("session-1", cursor="bad-cursor")


def test_list_results_skips_invalid_media_and_applies_score_threshold(image_search_tables, monkeypatch):
    monkeypatch.setattr(ImageSearchService, "_now", staticmethod(lambda: datetime(2026, 3, 13, 9, 0, 0)))
    invalid_thumbnail = _create_thumbnail("ABC-010", 10, valid=False)
    valid_thumbnail = _create_thumbnail("ABC-011", 20, valid=True)
    store = _DummyStore(
        {
            0: [
                _hit(invalid_thumbnail.id, invalid_thumbnail.media_id, invalid_thumbnail.media.movie.id, 10, 0.9),
                _hit(valid_thumbnail.id, valid_thumbnail.media_id, valid_thumbnail.media.movie.id, 20, 0.8),
            ]
        }
    )
    monkeypatch.setattr("src.service.discovery.image_search_service.settings.image_search.search_scan_batch_size", 4)
    service = ImageSearchService(store=store, embedder=_DummyEmbedder())
    session = ImageSearchSession.create(
        session_id="session-2",
        page_size=1,
        query_vector=[0.1, 0.2, 0.3],
        score_threshold=0.7,
        expires_at=datetime(2026, 3, 13, 10, 0, 0),
    )

    page = service.list_results(session.session_id)

    assert [item.thumbnail_id for item in page.items] == [valid_thumbnail.id]
    assert page.next_cursor is None

def test_build_item_applies_score_threshold_with_cosine_scaled_score(image_search_tables):
    thumbnail = _create_thumbnail("ABC-020", 20, valid=True)

    item = ImageSearchService._build_item(
        _hit(thumbnail.id, thumbnail.media_id, thumbnail.media.movie.id, 20, 0.5),
        thumbnail,
        0.6,
    )

    assert item is None
