from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from PIL import Image as PillowImage

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor, Image, Movie, MovieActor, MoviePlotImage, MovieTag, Tag
from src.model.catalog.movies import MOVIE_ACTOR_LIST_FIELD
from src.plugins.extensions.metadata import (
    PluginMetadataActor,
    PluginMovieMetadata,
)
from src.service.catalog.actor_resolution_service import ActorResolutionService
from src.service.catalog.metadata_source_service import (
    MetadataSourceError,
    MetadataSourceService,
)
from src.service.catalog.movie_metadata_refresh_service import (
    MovieMetadataRefreshService,
)


def _write_image(path: Path, *, color: str = "red", size=(600, 800)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    PillowImage.new("RGB", size, color).save(path, format="PNG")
    return str(path)


def _detail(
    tmp_path: Path,
    *,
    title: str = "新标题",
    summary: str = "新简介",
    maker_name: str | None = "新片商",
    director_name: str | None = "新导演",
    series_name: str | None = "新系列",
    actors: list[str] | None = None,
    tags: list[str] | None = None,
    plots: int = 1,
) -> PluginMovieMetadata:
    return PluginMovieMetadata(
        movie_number="ABC-001",
        title=title,
        release_date=date(2025, 2, 3),
        duration_minutes=150,
        cover_image_path=_write_image(tmp_path / "delivery" / "cover.png"),
        summary=summary,
        maker_name=maker_name,
        director_name=director_name,
        series_name=series_name,
        actors=[
            PluginMetadataActor(name=name)
            for name in (["新演员"] if actors is None else actors)
        ],
        tags=list(tags if tags is not None else ["新标签"]),
        plot_image_paths=[
            _write_image(
                tmp_path / "delivery" / f"plot-{index}.png",
                color="blue",
                size=(800, 600),
            )
            for index in range(plots)
        ],
    )


@pytest.fixture
def refresh_env(test_db, tmp_path, monkeypatch):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(
        settings.media, "import_image_root_path", str(image_root)
    )
    monkeypatch.setattr(
        ActorResolutionService,
        "ensure_avatar",
        classmethod(lambda *_args, **_kwargs: None),
    )
    movie = Movie.create(
        movie_number="ABC-001",
        title="旧标题",
        summary="旧简介",
        release_date=date(2024, 1, 2),
        duration_minutes=120,
        maker_name="旧片商",
        director_name="旧导演",
    )
    return movie, image_root, tmp_path


def _old_image(image_root: Path, origin: str) -> Image:
    _write_image(image_root / origin)
    return Image.create(origin=origin)


def test_refresh_updates_managed_metadata_and_cleans_old_images(refresh_env):
    movie, image_root, tmp_path = refresh_env
    old_cover = _old_image(image_root, "movies/00/ABC-001/cover-old.png")
    old_plot = _old_image(image_root, "movies/00/ABC-001/plot-old.png")
    movie.cover_image = old_cover
    movie.save(only=[Movie.cover_image])
    MoviePlotImage.create(movie=movie, image=old_plot)
    old_actor = Actor.create(name="旧演员")
    MovieActor.create(movie=movie, actor=old_actor)
    old_tag = Tag.create(name="旧标签")
    MovieTag.create(movie=movie, tag=old_tag)

    source = {"plugin_id": "new-source", "display_name": "New source"}
    refreshed = MovieMetadataRefreshService._refresh_records(
        movie,
        _detail(tmp_path, actors=["新演员"], tags=["新标签"], plots=2),
        source,
    )

    assert refreshed.title == "新标题"
    assert refreshed.summary == "新简介"
    assert refreshed.release_date.date() == date(2025, 2, 3)
    assert refreshed.duration_minutes == 150
    assert refreshed.maker_name == "新片商"
    assert refreshed.director_name == "新导演"
    assert refreshed.series.name == "新系列"
    assert refreshed.metadata_source == source
    assert {item.name for item in Actor.select().join(MovieActor).where(MovieActor.movie == refreshed)} == {
        "新演员"
    }
    assert {item.name for item in Tag.select().join(MovieTag).where(MovieTag.movie == refreshed)} == {
        "新标签"
    }
    assert refreshed.cover_image_id != old_cover.id
    assert MoviePlotImage.select().where(MoviePlotImage.movie == refreshed).count() == 2
    assert not Image.select().where(Image.id.in_([old_cover.id, old_plot.id])).exists()
    assert not Path(image_root, old_cover.origin).exists()
    assert not Path(image_root, old_plot.origin).exists()


def test_refresh_preserves_empty_optional_values_and_relations(refresh_env):
    movie, image_root, tmp_path = refresh_env
    old_cover = _old_image(image_root, "movies/00/ABC-001/cover-old.png")
    old_plot = _old_image(image_root, "movies/00/ABC-001/plot-old.png")
    movie.cover_image = old_cover
    movie.series = Movie.resolve_series("旧系列")
    movie.save(only=[Movie.cover_image, Movie.series])
    MoviePlotImage.create(movie=movie, image=old_plot)
    old_actor = Actor.create(name="旧演员")
    MovieActor.create(movie=movie, actor=old_actor)
    old_tag = Tag.create(name="旧标签")
    MovieTag.create(movie=movie, tag=old_tag)

    refreshed = MovieMetadataRefreshService._refresh_records(
        movie,
        _detail(
            tmp_path,
            summary="",
            maker_name=None,
            director_name=None,
            series_name=None,
            actors=[],
            tags=[],
            plots=0,
        ),
        {"plugin_id": "new-source"},
    )

    assert refreshed.title == "新标题"
    assert refreshed.summary == "旧简介"
    assert refreshed.maker_name == "旧片商"
    assert refreshed.director_name == "旧导演"
    assert refreshed.series.name == "旧系列"
    assert {item.name for item in Actor.select().join(MovieActor).where(MovieActor.movie == refreshed)} == {
        "旧演员"
    }
    assert {item.name for item in Tag.select().join(MovieTag).where(MovieTag.movie == refreshed)} == {
        "旧标签"
    }
    assert [link.image_id for link in MoviePlotImage.select().where(MoviePlotImage.movie == refreshed)] == [
        old_plot.id
    ]
    assert refreshed.cover_image_id != old_cover.id


def test_refresh_respects_manual_and_plugin_owned_fields_and_images(refresh_env):
    movie, image_root, tmp_path = refresh_env
    old_cover = _old_image(image_root, "movies/00/ABC-001/cover-old.png")
    old_plot = _old_image(image_root, "movies/00/ABC-001/plot-old.png")
    old_actor = Actor.create(name="人工演员")
    MovieActor.create(movie=movie, actor=old_actor)
    MoviePlotImage.create(movie=movie, image=old_plot)
    movie.cover_image = old_cover
    movie.field_owners = {
        "title": "plugin:text-owner",
        MOVIE_ACTOR_LIST_FIELD: "host:manual",
        "cover_image": "host:manual",
        "plot_images": "plugin:image-owner",
    }
    movie.save(only=[Movie.cover_image, Movie.field_owners])

    refreshed = MovieMetadataRefreshService._refresh_records(
        movie,
        _detail(tmp_path, actors=["新演员"], tags=["新标签"], plots=1),
        {"plugin_id": "new-source"},
    )

    assert refreshed.title == "旧标题"
    assert refreshed.summary == "新简介"
    assert refreshed.cover_image_id == old_cover.id
    assert [link.image_id for link in MoviePlotImage.select().where(MoviePlotImage.movie == refreshed)] == [
        old_plot.id
    ]
    assert [item.name for item in Actor.select().join(MovieActor).where(MovieActor.movie == refreshed)] == [
        "人工演员"
    ]
    assert [item.name for item in Tag.select().join(MovieTag).where(MovieTag.movie == refreshed)] == [
        "新标签"
    ]


def test_refresh_rejects_when_no_metadata_source_is_enabled(
    refresh_env, monkeypatch
):
    monkeypatch.setattr(
        MetadataSourceService,
        "enabled_plugin_sources",
        classmethod(lambda _cls: ()),
    )

    with pytest.raises(ApiError) as exc_info:
        MovieMetadataRefreshService.refresh_movie_metadata("ABC-001")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "metadata_source_unavailable"


def test_refresh_maps_not_found_and_source_failures(refresh_env, monkeypatch):
    movie, _image_root, _tmp_path = refresh_env
    monkeypatch.setattr(
        MetadataSourceService,
        "enabled_plugin_sources",
        classmethod(lambda _cls: (("source",),)),
    )

    @contextmanager
    def missing(_movie_number):
        raise MetadataNotFoundError("movie", movie.movie_number)
        yield

    monkeypatch.setattr(
        MetadataSourceService,
        "fetch",
        classmethod(lambda _cls, _movie_number: missing(movie.movie_number)),
    )
    with pytest.raises(ApiError) as not_found:
        MovieMetadataRefreshService.refresh_movie_metadata(movie.movie_number)
    assert not_found.value.status_code == 404
    assert not_found.value.code == "movie_metadata_not_found"

    @contextmanager
    def broken(_movie_number):
        raise MetadataSourceError("source failed")
        yield

    monkeypatch.setattr(
        MetadataSourceService,
        "fetch",
        classmethod(lambda _cls, _movie_number: broken(movie.movie_number)),
    )
    with pytest.raises(ApiError) as failure:
        MovieMetadataRefreshService.refresh_movie_metadata(movie.movie_number)
    assert failure.value.status_code == 502
    assert failure.value.code == "movie_metadata_refresh_failed"
