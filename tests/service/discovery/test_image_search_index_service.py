from pathlib import Path
from types import SimpleNamespace

import pytest

from src.model import (
    Image,
    ImageSearchIndexState,
    Media,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
)
from src.service.discovery.embedding_client import EmbeddingClientError
from src.service.discovery.image_search_index_service import ImageSearchIndexService
from src.service.discovery.image_search_index_space_service import (
    ImageSearchIndexRebuildRequiredError,
)


class _PendingQuery:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.limit_value: int | None = None

    def join(self, *_args, **_kwargs):
        return self

    def switch(self, *_args, **_kwargs):
        return self

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def __iter__(self):
        return iter(SimpleNamespace(id=item) for item in self.ids[: self.limit_value])


class _Store:
    def __init__(self, name: str, trace: list[str] | None = None) -> None:
        self.name = name
        self.trace = trace
        self.ensure_table_calls = 0
        self.ensure_scalar_indices_calls = 0
        self.batches = []

    def ensure_table(self, vector_size: int) -> None:
        assert vector_size == 2
        self.ensure_table_calls += 1

    def ensure_scalar_indices(self) -> None:
        self.ensure_scalar_indices_calls += 1

    def upsert_records(self, records) -> None:
        self.batches.append(list(records))
        if self.trace is not None:
            self.trace.append(self.name)


class _Embedder:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    @staticmethod
    def describe():
        return SimpleNamespace(space_id="siglip2-current", dimension=2)

    def embed_images(self, payloads):
        self.batch_sizes.append(len(payloads))
        return [[0.2, 0.3] for _ in payloads]


def _create_image(origin: str) -> Image:
    return Image.create(origin=origin, small=origin, medium=origin, large=origin)


def _prepare_images(tmp_path: Path, *, thumbnail_count: int, plot_count: int):
    movie = Movie.create(movie_number="INDEX-001", javdb_id="index-1", title="movie")
    library = MediaLibrary.create(
        name="index-library", provider_key="test", provider_config={}
    )
    media = Media.create(movie=movie, library=library, file_name="index.mp4")
    thumbnails = []
    plot_images = []
    paths: dict[str, Path] = {}
    for index in range(thumbnail_count):
        origin = f"movies/thumbnail-{index}.jpg"
        path = tmp_path / f"thumbnail-{index}.jpg"
        path.write_bytes(f"thumbnail-{index}".encode())
        paths[origin] = path
        thumbnails.append(
            MediaThumbnail.create(
                media=media,
                image=_create_image(origin),
                offset=index,
            )
        )
    for index in range(plot_count):
        origin = f"movies/plot-{index}.jpg"
        path = tmp_path / f"plot-{index}.jpg"
        path.write_bytes(f"plot-{index}".encode())
        paths[origin] = path
        plot_images.append(
            MoviePlotImage.create(movie=movie, image=_create_image(origin))
        )
    return thumbnails, plot_images, paths


def test_pending_queries_apply_work_batch_limit(monkeypatch):
    thumbnail_query = _PendingQuery([1, 2, 3])
    plot_query = _PendingQuery([1, 2, 3])
    monkeypatch.setattr(MediaThumbnail, "select", lambda *_args: thumbnail_query)
    monkeypatch.setattr(MoviePlotImage, "select", lambda *_args: plot_query)

    assert [item.id for item in ImageSearchIndexService._pending_thumbnails(2)] == [
        1,
        2,
    ]
    assert [item.id for item in ImageSearchIndexService._pending_plot_images(2)] == [
        1,
        2,
    ]
    assert thumbnail_query.limit_value == 2
    assert plot_query.limit_value == 2


def test_index_task_drains_both_queues_in_bounded_round_robin_batches(
    test_db, monkeypatch, tmp_path
):
    thumbnails, plot_images, paths = _prepare_images(
        tmp_path, thumbnail_count=3, plot_count=3
    )
    monkeypatch.setattr(
        "src.service.discovery.image_search_index_service.resolve_image_file_path",
        lambda origin: paths[origin],
    )
    monkeypatch.setattr(
        "src.config.config.settings.image_search.index_upsert_batch_size", 2
    )
    monkeypatch.setattr(
        "src.config.config.settings.image_search.inference_batch_size", 1
    )
    trace: list[str] = []
    thumbnail_store = _Store("thumbnail", trace)
    plot_store = _Store("plot", trace)
    embedder = _Embedder()

    stats = ImageSearchIndexService(
        store=thumbnail_store,
        plot_store=plot_store,
        embedder=embedder,
    ).index_pending_images()

    assert stats == {
        "processed_thumbnails": 3,
        "successful_thumbnails": 3,
        "failed_thumbnails": 0,
        "processed_plot_images": 3,
        "successful_plot_images": 3,
        "failed_plot_images": 0,
    }
    assert trace == ["thumbnail", "plot", "thumbnail", "plot"]
    assert [len(batch) for batch in thumbnail_store.batches] == [2, 1]
    assert [len(batch) for batch in plot_store.batches] == [2, 1]
    assert embedder.batch_sizes == [1, 1, 1, 1, 1, 1]
    assert thumbnail_store.ensure_table_calls == 1
    assert plot_store.ensure_table_calls == 1
    assert all(
        item.image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
        for item in MediaThumbnail.select().where(
            MediaThumbnail.id.in_([item.id for item in thumbnails])
        )
    )
    assert all(
        item.image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
        for item in MoviePlotImage.select().where(
            MoviePlotImage.id.in_([item.id for item in plot_images])
        )
    )


def test_batch_422_falls_back_to_single_images_and_only_fails_bad_image(
    test_db, monkeypatch, tmp_path
):
    thumbnails, _, paths = _prepare_images(tmp_path, thumbnail_count=2, plot_count=0)
    bad_payload = paths[thumbnails[1].image.origin].read_bytes()
    monkeypatch.setattr(
        "src.service.discovery.image_search_index_service.resolve_image_file_path",
        lambda origin: paths[origin],
    )
    monkeypatch.setattr(
        "src.config.config.settings.image_search.inference_batch_size", 2
    )

    class _RejectingEmbedder(_Embedder):
        def embed_images(self, payloads):
            if len(payloads) > 1 or payloads[0] == bad_payload:
                raise EmbeddingClientError(422, "invalid_image", "invalid image")
            return [[0.2, 0.3]]

    service = ImageSearchIndexService(
        store=_Store("thumbnail"),
        plot_store=_Store("plot"),
        embedder=_RejectingEmbedder(),
    )

    stats = service.index_pending_images()

    assert stats["successful_thumbnails"] == 1
    assert stats["failed_thumbnails"] == 1
    assert (
        MediaThumbnail.get_by_id(thumbnails[0].id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert (
        MediaThumbnail.get_by_id(thumbnails[1].id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED
    )


def test_qdrant_failure_leaves_batch_pending(test_db, monkeypatch, tmp_path):
    thumbnails, _, paths = _prepare_images(tmp_path, thumbnail_count=1, plot_count=0)
    monkeypatch.setattr(
        "src.service.discovery.image_search_index_service.resolve_image_file_path",
        lambda origin: paths[origin],
    )

    class _FailingStore(_Store):
        def upsert_records(self, records) -> None:
            raise RuntimeError("qdrant unavailable")

    service = ImageSearchIndexService(
        store=_FailingStore("thumbnail"),
        plot_store=_Store("plot"),
        embedder=_Embedder(),
    )

    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        service.index_pending_images()

    assert (
        MediaThumbnail.get_by_id(thumbnails[0].id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
    )


def test_index_task_blocks_mismatched_space_before_writing(
    test_db, monkeypatch, tmp_path
):
    thumbnails, _, paths = _prepare_images(tmp_path, thumbnail_count=1, plot_count=0)
    ImageSearchIndexState.create(id=1, indexed_space_id="siglip2-previous")
    monkeypatch.setattr(
        "src.service.discovery.image_search_index_service.resolve_image_file_path",
        lambda origin: paths[origin],
    )
    thumbnail_store = _Store("thumbnail")

    with pytest.raises(ImageSearchIndexRebuildRequiredError):
        ImageSearchIndexService(
            store=thumbnail_store,
            plot_store=_Store("plot"),
            embedder=_Embedder(),
        ).index_pending_images()

    assert thumbnail_store.batches == []
    assert (
        MediaThumbnail.get_by_id(thumbnails[0].id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
    )
