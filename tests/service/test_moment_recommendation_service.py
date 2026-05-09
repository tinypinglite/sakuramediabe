from datetime import datetime
from pathlib import Path

import pytest

from src.config.config import settings
from src.model import Image, Media, MediaPoint, MediaThumbnail, MomentRecommendation, Movie, MovieSimilarity
from src.service.discovery.lancedb_thumbnail_store import ThumbnailVectorSearchHit
from src.service.discovery.moment_recommendation_service import (
    MomentRecommendationService,
    STRATEGY_POPULAR,
    STRATEGY_SIMILAR_MOVIE,
    STRATEGY_VISUAL,
)


class _DummyEmbedder:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def infer_image_bytes(self, _image_bytes: bytes):
        if self.fail:
            raise ValueError("inference failed")

        class _Result:
            vector = [0.1, 0.2, 0.3]

        return _Result()


class _DummyStore:
    def __init__(self, hits=None, *, fail: bool = False):
        self.hits = hits or []
        self.fail = fail
        self.calls = []

    def search(self, query_vector, limit=20, offset=0, movie_ids=None, exclude_movie_ids=None):
        self.calls.append(
            {
                "query_vector": query_vector,
                "limit": limit,
                "offset": offset,
                "movie_ids": movie_ids,
                "exclude_movie_ids": exclude_movie_ids,
            }
        )
        if self.fail:
            raise RuntimeError("store failed")
        return self.hits


@pytest.fixture(autouse=True)
def _image_root(tmp_path, monkeypatch, app):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    return tmp_path


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _create_thumbnail(movie: Movie, offset: int, *, duration: int = 1000, valid: bool = True, root: Path):
    media = Media.create(
        movie=movie,
        path=f"/library/{movie.movie_number}-{offset}.mp4",
        valid=valid,
        duration_seconds=duration,
    )
    relative_path = f"movies/{movie.movie_number}/thumb-{offset}.webp"
    image_path = root / relative_path
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image")
    image = Image.create(
        origin=relative_path,
        small=relative_path,
        medium=relative_path,
        large=relative_path,
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=offset)
    return media, thumbnail


def _hit(thumbnail: MediaThumbnail, score: float):
    return ThumbnailVectorSearchHit(
        thumbnail_id=thumbnail.id,
        media_id=thumbnail.media_id,
        movie_id=thumbnail.media.movie.id,
        offset_seconds=thumbnail.offset,
        score=score,
    )


def test_generate_recommendations_uses_visual_candidates_and_same_movie_penalty(_image_root):
    seed_movie = _create_movie("ABP-001", "javdb-seed", heat=10)
    target_movie = _create_movie("ABP-002", "javdb-target", heat=20)
    seed_media, seed_thumbnail = _create_thumbnail(seed_movie, 100, root=_image_root)
    _, same_movie_thumbnail = _create_thumbnail(seed_movie, 400, root=_image_root)
    _, target_thumbnail = _create_thumbnail(target_movie, 300, root=_image_root)
    MediaPoint.create(media=seed_media, thumbnail=seed_thumbnail, offset_seconds=100)
    store = _DummyStore([_hit(same_movie_thumbnail, 0.99), _hit(target_thumbnail, 0.80)])
    service = MomentRecommendationService(store=store, embedder=_DummyEmbedder())

    stats = service.generate_recommendations(limit=2)
    rows = list(MomentRecommendation.select().order_by(MomentRecommendation.rank.asc()))

    assert stats["seed_points"] == 1
    assert stats["visual_candidates"] == 2
    assert [row.thumbnail_id for row in rows] == [target_thumbnail.id, same_movie_thumbnail.id]
    assert all(row.strategy == STRATEGY_VISUAL for row in rows)
    assert rows[0].score > rows[1].score


def test_generate_recommendations_filters_collection_visual_candidates(_image_root):
    seed_movie = _create_movie("ABP-005", "javdb-seed-collection")
    collection_movie = _create_movie(
        "COLLECTION-001",
        "javdb-collection-hit",
        is_collection=True,
        heat=100,
    )
    normal_movie = _create_movie("ABP-006", "javdb-normal-hit", heat=10)
    seed_media, seed_thumbnail = _create_thumbnail(seed_movie, 100, root=_image_root)
    _, collection_thumbnail = _create_thumbnail(collection_movie, 200, root=_image_root)
    _, normal_thumbnail = _create_thumbnail(normal_movie, 300, root=_image_root)
    MediaPoint.create(media=seed_media, thumbnail=seed_thumbnail, offset_seconds=100)
    store = _DummyStore([_hit(collection_thumbnail, 0.99), _hit(normal_thumbnail, 0.60)])
    service = MomentRecommendationService(store=store, embedder=_DummyEmbedder())

    service.generate_recommendations(limit=1)
    rows = list(MomentRecommendation.select().order_by(MomentRecommendation.rank.asc()))

    assert [row.thumbnail_id for row in rows] == [normal_thumbnail.id]
    assert rows[0].movie_id == normal_movie.id


def test_generate_recommendations_falls_back_to_similar_movie_candidates(_image_root):
    seed_movie = _create_movie("ABP-010", "javdb-seed")
    similar_movie = _create_movie("ABP-011", "javdb-similar", heat=30)
    seed_media, seed_thumbnail = _create_thumbnail(seed_movie, 200, duration=1000, root=_image_root)
    similar_media, similar_thumbnail = _create_thumbnail(similar_movie, 220, duration=1000, root=_image_root)
    _create_thumbnail(similar_movie, 800, duration=1000, root=_image_root)
    MediaPoint.create(media=seed_media, thumbnail=seed_thumbnail, offset_seconds=200)
    MovieSimilarity.create(source_movie=seed_movie, target_movie=similar_movie, score=0.9, rank=1)
    service = MomentRecommendationService(store=_DummyStore([], fail=True), embedder=_DummyEmbedder(fail=True))

    stats = service.generate_recommendations(limit=5)
    row = MomentRecommendation.get()

    assert stats["visual_candidates"] == 0
    assert stats["similar_candidates"] == 1
    assert row.strategy == STRATEGY_SIMILAR_MOVIE
    assert row.thumbnail_id == similar_thumbnail.id
    assert row.media_id == similar_media.id
    assert row.movie_similarity_score == 0.9


def test_generate_recommendations_uses_popular_candidates_without_seed(_image_root):
    low = _create_movie("ABP-020", "javdb-low", heat=5)
    high = _create_movie("ABP-021", "javdb-high", heat=90)
    _create_thumbnail(low, 100, duration=1000, root=_image_root)
    _, high_thumbnail = _create_thumbnail(high, 350, duration=1000, root=_image_root)
    service = MomentRecommendationService(store=_DummyStore([]), embedder=_DummyEmbedder())

    stats = service.generate_recommendations(limit=5)
    row = MomentRecommendation.get(MomentRecommendation.rank == 1)

    assert stats["seed_points"] == 0
    assert stats["popular_candidates"] >= 1
    assert row.strategy == STRATEGY_POPULAR
    assert row.thumbnail_id == high_thumbnail.id


def test_generate_recommendations_uses_later_media_when_first_media_has_no_thumbnail(_image_root):
    movie = _create_movie("ABP-025", "javdb-later-media", heat=90)
    Media.create(movie=movie, path="/library/abp-025-first.mp4", valid=True, duration_seconds=1000)
    second_media, second_thumbnail = _create_thumbnail(movie, 350, duration=1000, root=_image_root)
    service = MomentRecommendationService(store=_DummyStore([]), embedder=_DummyEmbedder())

    stats = service.generate_recommendations(limit=1)
    row = MomentRecommendation.get()

    assert stats["stored_items"] == 1
    assert row.media_id == second_media.id
    assert row.thumbnail_id == second_thumbnail.id


def test_generate_recommendations_replaces_old_pool_and_clears_old_on_empty(_image_root):
    old_movie = _create_movie("ABP-030", "javdb-old")
    _, old_thumbnail = _create_thumbnail(old_movie, 100, root=_image_root)
    MomentRecommendation.create(
        rank=1,
        score=0.1,
        strategy=STRATEGY_POPULAR,
        reason="old",
        movie=old_movie,
        media=old_thumbnail.media,
        thumbnail=old_thumbnail,
        offset_seconds=old_thumbnail.offset,
        generated_at=datetime(2026, 5, 7, 1, 0, 0),
    )

    MomentRecommendationService(store=_DummyStore([]), embedder=_DummyEmbedder()).generate_recommendations(limit=0)
    assert MomentRecommendation.select().count() == 0

    new_movie = _create_movie("ABP-031", "javdb-new", heat=90)
    _, new_thumbnail = _create_thumbnail(new_movie, 350, root=_image_root)

    MomentRecommendationService(store=_DummyStore([]), embedder=_DummyEmbedder()).generate_recommendations(limit=1)
    rows = list(MomentRecommendation.select())
    assert len(rows) == 1
    assert rows[0].thumbnail_id == new_thumbnail.id


def test_list_items_filters_invalid_media_before_pagination(_image_root):
    invalid_movie = _create_movie("ABP-040", "javdb-invalid", heat=90)
    first_valid_movie = _create_movie("ABP-041", "javdb-valid-1", heat=80)
    second_valid_movie = _create_movie("ABP-042", "javdb-valid-2", heat=70)
    _, invalid_thumbnail = _create_thumbnail(invalid_movie, 100, valid=False, root=_image_root)
    _, first_valid_thumbnail = _create_thumbnail(first_valid_movie, 200, root=_image_root)
    _, second_valid_thumbnail = _create_thumbnail(second_valid_movie, 300, root=_image_root)
    generated_at = datetime(2026, 5, 8, 4, 0, 0)
    for rank, movie, thumbnail in [
        (1, invalid_movie, invalid_thumbnail),
        (2, first_valid_movie, first_valid_thumbnail),
        (3, second_valid_movie, second_valid_thumbnail),
    ]:
        MomentRecommendation.create(
            rank=rank,
            score=1.0 - (rank * 0.1),
            strategy=STRATEGY_POPULAR,
            reason="热门",
            movie=movie,
            media=thumbnail.media,
            thumbnail=thumbnail,
            offset_seconds=thumbnail.offset,
            generated_at=generated_at,
        )

    first_page = MomentRecommendationService.list_items(page=1, page_size=1)
    second_page = MomentRecommendationService.list_items(page=2, page_size=1)

    assert first_page.total == 2
    assert first_page.items[0].recommendation_id == MomentRecommendation.get(MomentRecommendation.rank == 2).id
    assert first_page.items[0].movie.movie_number == "ABP-041"
    assert second_page.total == 2
    assert second_page.items[0].movie.movie_number == "ABP-042"
