"""插件契约回归测试：MovieSnapshot / context.movies / import_movie_by_number。

覆盖 v2-lite 设计文档第 4 节的保证：读取与导入只返回不可变快照、patch 走
唯一网关。插件 Host API 只接受当前版本。白名单当前真实开放
title / summary / is_collection。
"""

from __future__ import annotations

import pytest

from src.model import Actor, Movie, MovieActor, MovieTag, Tag
from src.plugins import MOVIE_SNAPSHOT_FIELDS, MoviePage, MovieSnapshot
from src.plugins.context import MovieApi
from src.plugins.contracts import HOST_API_VERSION, MIN_SUPPORTED_HOST_API_VERSION
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins


def _create_movie(test_db, **overrides) -> Movie:
    fields = {
        "javdb_id": "javdb-1",
        "movie_number": "ABP-001",
        "title": "旧标题",
        "summary": "旧简介",
    }
    fields.update(overrides)
    return Movie.create(**fields)


def test_snapshot_contains_only_public_readonly_fields(test_db):
    movie = _create_movie(test_db, extra={"secret": "x"}, heat=999)
    snapshot = MovieApi._to_snapshot(movie)

    assert isinstance(snapshot, MovieSnapshot)
    assert set(snapshot.values) == set(MOVIE_SNAPSHOT_FIELDS)
    # 新列（extra/heat 等）不会因默认行为意外暴露给插件。
    assert "extra" not in snapshot.values
    assert "heat" not in snapshot.values
    assert "field_owners" not in snapshot.values
    assert snapshot.values["title"] == "旧标题"
    assert snapshot.movie_id == movie.id
    assert snapshot.revision == 0
    assert snapshot.owners == {}


def test_snapshot_is_immutable(test_db):
    from dataclasses import FrozenInstanceError

    movie = _create_movie(test_db)
    snapshot = MovieApi._to_snapshot(movie)

    with pytest.raises(FrozenInstanceError):
        snapshot.movie_id = 99
    with pytest.raises(TypeError):
        snapshot.values["title"] = "被插件改坏"
    with pytest.raises(TypeError):
        snapshot.owners["title"] = "plugin:other"
    fresh = Movie.get_by_id(movie.id)
    assert fresh.title == "旧标题"


def test_movies_get_returns_snapshot_or_none(test_db):
    movie = _create_movie(test_db)
    api = MovieApi("demo_plugin")

    snapshot = api.get(movie.id)
    assert snapshot is not None
    assert snapshot.values["movie_number"] == "ABP-001"
    assert api.get(999999) is None


def test_movies_find_by_numbers_is_case_and_separator_insensitive(test_db):
    _create_movie(test_db, movie_number="ABP-001")
    _create_movie(test_db, javdb_id="javdb-2", movie_number="072625_001")
    api = MovieApi("demo_plugin")

    # 大小写不敏感 + 下划线/连字符候选，与人工输入点查同语义。
    snapshots = api.find_by_numbers(["abp-001"])
    assert [snapshot.values["movie_number"] for snapshot in snapshots] == ["ABP-001"]

    snapshots = api.find_by_numbers(["072625-001"])
    assert [snapshot.values["movie_number"] for snapshot in snapshots] == ["072625_001"]

    # 找不到的番号跳过；重复番号按输入顺序去重。
    snapshots = api.find_by_numbers(["abp-001", "ZZZ-999", "ABP-001"])
    assert [snapshot.values["movie_number"] for snapshot in snapshots] == ["ABP-001"]


def test_movies_list_page_walks_full_library_by_id_cursor(test_db):
    first = _create_movie(test_db, movie_number="ABP-001")
    second = _create_movie(
        test_db,
        javdb_id="javdb-2",
        movie_number="ABP-002",
    )
    third = _create_movie(
        test_db,
        javdb_id="javdb-3",
        movie_number="ABP-003",
    )
    api = MovieApi("demo_plugin")

    first_page = api.list_page(limit=2)
    assert isinstance(first_page, MoviePage)
    assert [item.movie_id for item in first_page.items] == [first.id, second.id]
    assert first_page.next_cursor == second.id

    second_page = api.list_page(after_id=first_page.next_cursor, limit=2)
    assert [item.movie_id for item in second_page.items] == [third.id]
    assert second_page.next_cursor is None

def test_movies_patch_goes_through_gateway(test_db):
    movie = _create_movie(test_db)
    api = MovieApi("demo_plugin")

    assert api.patch(movie.id, {"title": "插件标题"}, expected_revision=0) is True
    assert api.patch(
        movie.id,
        {"is_collection": True},
        expected_revision=1,
    ) is True
    snapshot = api.get(movie.id)
    assert snapshot.values["title"] == "插件标题"
    assert snapshot.values["is_collection"] is True
    assert snapshot.owners == {
        "title": "plugin:demo_plugin",
        "is_collection": "plugin:demo_plugin",
    }
    assert snapshot.revision == 2

    # 陈旧 revision：返回 False 且零修改。
    assert api.patch(movie.id, {"title": "再次写入"}, expected_revision=0) is False
    assert api.get(movie.id).values["title"] == "插件标题"

    # 非法字段（不在白名单）直接抛错，不静默。
    with pytest.raises(ValueError, match="非受保护字段"):
        api.patch(movie.id, {"heat": 999}, expected_revision=1)


def test_movie_page_batches_relations_without_cross_movie_leaks(test_db):
    from playhouse.test_utils import count_queries

    actor = Actor.create(javdb_id="actor-1", name="演员", height_cm=168)
    tag = Tag.create(name="标签")
    movies = [
        _create_movie(
            test_db, javdb_id=f"j-{i}", movie_number=f"ABP-{i}",
            series_name="系列", is_blacklisted=True,
        )
        for i in range(5)
    ]
    for movie in movies[1:]:
        MovieActor.create(movie=movie, actor=actor)
        MovieTag.create(movie=movie, tag=tag)
    with count_queries(only_select=True) as queries:
        page = MovieApi("demo_plugin").list_page(limit=4)
    assert queries.count <= 4  # 防止逐部查询关联；允许后续减少查询。
    assert page.next_cursor == movies[3].id
    assert page.items[0].actors == ()
    assert page.items[0].tags == ()
    for snapshot in page.items[1:]:
        assert snapshot.values["is_blacklisted"] is True
        assert snapshot.values["series_name"] == "系列"
        assert [item.actor_id for item in snapshot.actors] == [actor.id]
        assert snapshot.actors[0].values["height_cm"] == 168
        assert [item.tag_id for item in snapshot.tags] == [tag.id]


def test_import_movie_by_number_returns_snapshot(test_db, monkeypatch):
    from types import SimpleNamespace

    import src.plugins.context as context_module

    movie = Movie.create(
        javdb_id="javdb-1",
        movie_number="ABP-001",
        title="导入标题",
        summary="导入简介",
    )
    actor = Actor.create(javdb_id="import-actor", name="导入演员")
    tag = Tag.create(name="导入标签")
    MovieActor.create(movie=movie, actor=actor)
    MovieTag.create(movie=movie, tag=tag)

    def fake_provider(self):
        return provider

    def fake_import_service(self):
        return importer

    provider = SimpleNamespace(get_movie_by_number=lambda number: {"title": number})
    importer = SimpleNamespace(
        import_movie_if_missing=lambda detail, force_subscribed=False: (movie, True)
    )

    monkeypatch.setattr(context_module.PluginContext, "build_javdb_provider", fake_provider)
    monkeypatch.setattr(context_module.PluginContext, "build_catalog_import_service", fake_import_service)

    from src.plugins.context import PluginContext

    context = PluginContext(plugin_id="demo_plugin", settings={}, data_dir="")
    snapshot = context.import_movie_by_number("ABP-001")

    assert isinstance(snapshot, MovieSnapshot)
    assert snapshot.movie_id == movie.id
    assert snapshot.values["title"] == "导入标题"
    assert snapshot.values["movie_number"] == "ABP-001"
    assert snapshot.revision == 0
    assert snapshot.owners == {}
    # 返回值不再携带任何可写 ORM 句柄。
    assert not hasattr(snapshot, "save")
    assert snapshot.actors[0].actor_id == actor.id
    assert snapshot.tags[0].tag_id == tag.id


def test_host_api_accepts_supported_manifest_versions(tmp_path):
    """v6 Host 在 import 前拒绝范围外 manifest，并保留 v4/v5 包兼容。"""
    import json

    from src.config.config import Plugins

    assert HOST_API_VERSION == 6
    assert MIN_SUPPORTED_HOST_API_VERSION == 4

    for plugin_id, declared in (
        ("unsupported_plugin", 2),
        ("legacy_plugin", MIN_SUPPORTED_HOST_API_VERSION),
        ("current_plugin", HOST_API_VERSION),
    ):
        pkg = tmp_path / plugin_id
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "manifest.json").write_text(
            json.dumps(
                {
                    "plugin_id": plugin_id,
                    "display_name": plugin_id,
                    "version": "1.0.0",
                    "host_api_version": declared,
                }
            ),
            encoding="utf-8",
        )
        registration_version = (
            str(MIN_SUPPORTED_HOST_API_VERSION)
            if plugin_id == "legacy_plugin"
            else "HOST_API_VERSION"
        )
        source = (
            "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
            "def register(context):\n"
            f"    return PluginRegistration(plugin_id={plugin_id!r}, display_name='x', version='1.0.0', "
            f"host_api_version={registration_version}, jobs=())\n"
        )
        (pkg / "__init__.py").write_text(source, encoding="utf-8")

    loaded = load_enabled_plugins(
        Plugins(enabled=["unsupported_plugin", "legacy_plugin", "current_plugin"]),
        root_dir=tmp_path,
    )
    assert [registration.plugin_id for registration in loaded] == ["legacy_plugin", "current_plugin"]
    assert PLUGIN_LOAD_ERRORS["unsupported_plugin"]["stage"] == "validate_manifest"
