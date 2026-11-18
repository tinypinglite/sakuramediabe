"""插件图片写入契约：封面、剧照、演员头像的显式替换与 owner 冲突。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PillowImage

from src.common.image_store import read_image_bytes
from src.config.config import settings
from src.model import Actor, Image, Movie, MoviePlotImage
from src.plugins import PluginContext
from src.service.catalog.actor_service import ActorService


def write_image(
    path: Path, *, color: str = "red", size=(600, 400), image_format: str = "PNG"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    PillowImage.new("RGB", size, color).save(path, format=image_format)
    return path


@pytest.fixture
def image_env(test_db, tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    monkeypatch.setattr(settings.plugins, "root_dir", str(plugins_root))
    monkeypatch.setattr(
        settings.media, "import_image_root_path", str(tmp_path / "assets")
    )
    movie = Movie.create(
        movie_number="ABC-001",
        title="影片",
        summary="",
    )
    return SimpleNamespace(root=plugins_root, movie=movie, tmp_path=tmp_path)


def plugin_context(env, plugin_id: str) -> PluginContext:
    return PluginContext(plugin_id, {}, env.root / plugin_id / "data")


def delivered(env, plugin_id: str, name: str, **kwargs) -> str:
    return str(
        write_image(env.root / plugin_id / "data" / "images" / name, **kwargs)
    )


def test_set_cover_replaces_image_takes_owner_and_cleans_old(test_db, image_env):
    movie = image_env.movie
    old_cover = Image.create(origin="movies/00/ABC-001/cover.jpg")
    movie.cover_image = old_cover
    movie.save(only=[Movie.cover_image])
    context = plugin_context(image_env, "cover_plugin")

    snapshot = context.movies.get(movie.id)
    assert context.movies.set_cover(
        movie.id, delivered(image_env, "cover_plugin", "new-cover.png"), snapshot.revision
    )

    fresh = Movie.get_by_id(movie.id)
    assert fresh.cover_image_id != old_cover.id
    assert fresh.field_owners["cover_image"] == "plugin:cover_plugin"
    assert fresh.mutation_revision == snapshot.revision + 1
    origin = fresh.cover_image.origin
    assert origin.startswith("movies/")
    assert origin.endswith(".png")
    # 显式替换写新路径，避免长缓存吃到旧图。
    assert Path(origin).name.startswith("cover-")
    assert read_image_bytes(origin)
    assert not Image.select().where(Image.id == old_cover.id).exists()


def test_set_cover_rejects_conflicts_and_stale_revision(test_db, image_env):
    movie = image_env.movie
    owner = plugin_context(image_env, "cover_plugin")
    other = plugin_context(image_env, "other_plugin")

    first = owner.movies.get(movie.id)
    assert owner.movies.set_cover(
        movie.id, delivered(image_env, "cover_plugin", "a.png"), first.revision
    )
    current_cover_id = Movie.get_by_id(movie.id).cover_image_id

    # 别的插件已被 owner 挡住；版本过期同样拒绝，且零修改。
    assert not other.movies.set_cover(
        movie.id, delivered(image_env, "other_plugin", "b.png"), first.revision + 1
    )
    assert not owner.movies.set_cover(
        movie.id, delivered(image_env, "cover_plugin", "c.png"), first.revision
    )
    assert Movie.get_by_id(movie.id).cover_image_id == current_cover_id


def test_set_cover_respects_manual_owner(test_db, image_env):
    movie = image_env.movie
    Movie.update(field_owners={"cover_image": "host:manual"}).where(
        Movie.id == movie.id
    ).execute()
    context = plugin_context(image_env, "cover_plugin")

    snapshot = context.movies.get(movie.id)
    assert not context.movies.set_cover(
        movie.id, delivered(image_env, "cover_plugin", "new.png"), snapshot.revision
    )
    assert Movie.get_by_id(movie.id).cover_image_id is None


def test_set_cover_validates_delivery_path_and_format(test_db, image_env):
    movie = image_env.movie
    context = plugin_context(image_env, "cover_plugin")
    snapshot = context.movies.get(movie.id)
    outside = write_image(image_env.tmp_path / "outside.png")
    with pytest.raises(ValueError):
        context.movies.set_cover(movie.id, str(outside), snapshot.revision)
    with pytest.raises(ValueError):
        context.movies.set_cover(
            movie.id,
            delivered(image_env, "cover_plugin", "cover.bmp"),
            snapshot.revision,
        )
    mismatch = write_image(
        image_env.root / "cover_plugin" / "data" / "images" / "cover.jpg",
        image_format="PNG",
    )
    with pytest.raises(ValueError):
        context.movies.set_cover(movie.id, str(mismatch), snapshot.revision)
    assert Movie.get_by_id(movie.id).cover_image_id is None


def test_set_plot_images_replaces_whole_set(test_db, image_env):
    movie = image_env.movie
    old_plot = Image.create(origin="movies/00/ABC-001/plot-0.jpg")
    MoviePlotImage.create(movie=movie, image=old_plot)
    context = plugin_context(image_env, "plot_plugin")
    paths = [
        delivered(image_env, "plot_plugin", "plot-a.png", color="blue"),
        delivered(image_env, "plot_plugin", "plot-b.png", color="green"),
    ]

    snapshot = context.movies.get(movie.id)
    assert context.movies.set_plot_images(movie.id, paths, snapshot.revision)

    fresh = Movie.get_by_id(movie.id)
    assert fresh.field_owners["plot_images"] == "plugin:plot_plugin"
    links = list(
        MoviePlotImage.select()
        .where(MoviePlotImage.movie == movie.id)
        .order_by(MoviePlotImage.id)
    )
    assert len(links) == 2
    assert all(
        link.image_search_index_status == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
        for link in links
    )
    assert all(read_image_bytes(link.image.origin) for link in links)
    assert not Image.select().where(Image.id == old_plot.id).exists()


def test_set_plot_images_empty_clears_and_takes_ownership(test_db, image_env):
    movie = image_env.movie
    old_plot = Image.create(origin="movies/00/ABC-001/plot-0.jpg")
    MoviePlotImage.create(movie=movie, image=old_plot)
    context = plugin_context(image_env, "plot_plugin")

    snapshot = context.movies.get(movie.id)
    assert context.movies.set_plot_images(movie.id, [], snapshot.revision)

    fresh = Movie.get_by_id(movie.id)
    assert fresh.field_owners["plot_images"] == "plugin:plot_plugin"
    assert MoviePlotImage.select().where(MoviePlotImage.movie == movie.id).count() == 0
    assert not Image.select().where(Image.id == old_plot.id).exists()


def test_set_profile_image_writes_override_and_clear_restores_base(test_db, image_env):
    actor = Actor.create(name="演员")
    base_image = Image.create(origin="actors/actor-image-1.jpg")
    actor.profile_image = base_image
    actor.save(only=[Actor.profile_image])
    context = plugin_context(image_env, "actor_plugin")

    snapshot = context.actors.get(actor.id)
    assert context.actors.set_profile_image(
        actor.id, delivered(image_env, "actor_plugin", "avatar.png"), snapshot.revision
    )

    fresh = Actor.get_by_id(actor.id)
    assert fresh.field_owners["profile_image_override"] == "plugin:actor_plugin"
    override = fresh.profile_image_override
    assert override is not None and override.id != base_image.id
    assert override.origin.endswith(".webp")
    assert read_image_bytes(override.origin)

    snapshot = context.actors.get(actor.id)
    assert context.actors.clear_profile_image(actor.id, snapshot.revision)

    fresh = Actor.get_by_id(actor.id)
    assert fresh.profile_image_override_id is None
    assert "profile_image_override" not in (fresh.field_owners or {})
    assert fresh.effective_profile_image.id == base_image.id
    assert not Image.select().where(Image.id == override.id).exists()


def test_profile_image_conflicts_keep_manual_and_other_plugins(test_db, image_env):
    actor = Actor.create(name="演员")
    png = BytesIO()
    PillowImage.new("RGB", (200, 300), "purple").save(png, format="PNG")
    ActorService.upload_profile_image(
        actor.id, content=png.getvalue(), content_type="image/png"
    )
    context = plugin_context(image_env, "actor_plugin")

    snapshot = context.actors.get(actor.id)
    assert not context.actors.set_profile_image(
        actor.id, delivered(image_env, "actor_plugin", "avatar.png"), snapshot.revision
    )
    assert Actor.get_by_id(actor.id).field_owners["profile_image_override"] == "host:manual"

    # 人工覆盖存在时插件也无法清除。
    assert not context.actors.clear_profile_image(actor.id, snapshot.revision)
    assert Actor.get_by_id(actor.id).profile_image_override_id is not None


@pytest.mark.parametrize("revision", [-1, "1", 1.5, True])
def test_image_writes_reject_invalid_revision(test_db, image_env, revision):
    """版本必须是整数：布尔/字符串会被 Postgres 隐式转成整数，匹配上错误版本。"""
    actor = Actor.create(name="演员")
    source = delivered(image_env, "cover_plugin", "cover.png")
    movie = image_env.movie

    with pytest.raises(ValueError):
        plugin_context(image_env, "cover_plugin").movies.set_cover(
            movie.id, source, revision
        )
    with pytest.raises(ValueError):
        plugin_context(image_env, "actor_plugin").actors.set_profile_image(
            actor.id, source, revision
        )

    assert Movie.get_by_id(movie.id).cover_image_id is None
    assert Actor.get_by_id(actor.id).profile_image_override_id is None
