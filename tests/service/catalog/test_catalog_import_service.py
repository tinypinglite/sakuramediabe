"""CatalogImportService 三函数语义护栏。

锁住三条导入语义，防止重构把它改回"全能 upsert"：

1. ``import_movie_if_missing``：纯新建——已存在影片一个字段都不写；
2. ``refresh_movie_metadata_strict``：纯覆盖——手动刷新入口，已存在影片全量覆盖；
3. ``update_movie_fields``：指定字段更新——不存在先完整导入，存在只写白名单字段。
"""

import pytest

from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import Actor, Movie
from src.service.catalog.catalog_import_service import CatalogImportService


def _build_detail(
    *,
    javdb_id: str,
    movie_number: str,
    title: str,
    summary: str,
    actors: list[JavdbMovieActorResource] | None = None,
) -> JavdbMovieDetailResource:
    """构造无图片的最小详情：封面/剧照/演员头像全空，导入链路零图片 IO。"""
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=title,
        summary=summary,
        duration_minutes=120,
        release_date="2024-01-01",
        score=9.5,
        score_number=200,
        watched_count=100,
        want_watch_count=10,
        comment_count=5,
        actors=actors or [],
        tags=[],
    )


def _create_local_movie(
    *,
    javdb_id: str,
    movie_number: str,
    title: str,
    summary: str,
    score: float = 1.0,
) -> Movie:
    return Movie.create(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=title,
        summary=summary,
        score=score,
    )


def test_import_movie_if_missing_skips_existing_movie_without_writing(test_db):
    """已存在影片：纯新建跳过，所有字段保持本地值。"""
    _create_local_movie(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="本地标题",
        summary="本地描述",
        score=1.0,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is False
    assert movie.id is not None
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.title == "本地标题"
    assert refreshed.summary == "本地描述"
    assert refreshed.score == 1.0


def test_import_movie_if_missing_creates_new_movie(test_db):
    """不存在影片：完整导入并返回 created=True。"""
    detail = _build_detail(
        javdb_id="javdb-ABP-456",
        movie_number="ABP-456",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is True
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.title == "JavDB标题"
    assert refreshed.summary == "JavDB描述"
    assert refreshed.score == 9.5
    assert refreshed.watched_count == 100


def test_import_movie_if_missing_does_not_mark_collection(test_db):
    detail = _build_detail(
        javdb_id="javdb-OFJE-456",
        movie_number="OFJE-456",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is True
    assert Movie.get_by_id(movie.id).is_collection is False


def test_import_movie_if_missing_updates_actor_gender_from_movie_detail(test_db):
    detail = _build_detail(
        javdb_id="javdb-ABP-457",
        movie_number="ABP-457",
        title="JavDB标题",
        summary="JavDB描述",
        actors=[
            JavdbMovieActorResource(
                javdb_id="actor-1",
                name="演员一",
                gender=1,
            )
        ],
    )

    CatalogImportService().import_movie_if_missing(detail)

    assert Actor.get(Actor.javdb_id == "actor-1").gender == 1


def test_actor_upsert_without_gender_update_preserves_existing_gender(test_db):
    actor = Actor.create(
        javdb_id="actor-2",
        name="旧名字",
        gender=1,
    )
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name="新名字",
        gender=0,
    )

    CatalogImportService().upsert_actor_from_javdb_resource(resource)

    refreshed = Actor.get_by_id(actor.id)
    assert refreshed.name == "新名字"
    assert refreshed.gender == 1


def test_strict_actor_refresh_preserves_existing_gender(test_db):
    actor = Actor.create(
        javdb_id="actor-3",
        name="旧名字",
        gender=1,
    )
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name="新名字",
        gender=0,
    )

    CatalogImportService()._refresh_actor_from_javdb_resource_strict(
        actor_resource=resource,
        profile_image_task=None,
    )

    refreshed = Actor.get_by_id(actor.id)
    assert refreshed.name == "新名字"
    assert refreshed.gender == 1


def test_update_movie_fields_updates_only_specified_fields_on_existing_movie(test_db):
    """已存在影片：只更新指定字段，其余字段保持本地值。"""
    _create_local_movie(
        javdb_id="javdb-ABP-789",
        movie_number="ABP-789",
        title="本地标题",
        summary="本地描述",
        score=1.0,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-789",
        movie_number="ABP-789",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("score", "watched_count"),
    )

    assert created is False
    assert updated_fields == ("score", "watched_count")
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.score == 9.5
    assert refreshed.watched_count == 100
    # 未指定字段保持本地值。
    assert refreshed.title == "本地标题"
    assert refreshed.summary == "本地描述"
    assert refreshed.want_watch_count == 0


def test_update_movie_fields_creates_missing_movie_before_updating(test_db):
    """不存在影片：先完整导入，再应用指定字段。"""
    detail = _build_detail(
        javdb_id="javdb-ABP-000",
        movie_number="ABP-000",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("comment_count",),
    )

    assert created is True
    # 完整导入已写入 detail 取值，变更检测判定无字段再变化。
    assert updated_fields == ()
    refreshed = Movie.get_by_id(movie.id)
    # 完整导入已写入全部字段。
    assert refreshed.title == "JavDB标题"
    assert refreshed.comment_count == 5


def test_update_movie_fields_skips_unchanged_values(test_db):
    """值无变化的字段不写库，updated_fields 为空（updated/unchanged 计数语义护栏）。"""
    _create_local_movie(
        javdb_id="javdb-ABP-002",
        movie_number="ABP-002",
        title="本地标题",
        summary="本地描述",
        score=9.5,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-002",
        movie_number="ABP-002",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("score", "title"),
    )

    assert created is False
    # score 与本地一致跳过；title 不同则写入。
    assert updated_fields == ("title",)
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.score == 9.5
    assert refreshed.title == "JavDB标题"
    assert refreshed.summary == "本地描述"


def test_update_movie_fields_rejects_fields_outside_whitelist(test_db):
    detail = _build_detail(
        javdb_id="javdb-ABP-001",
        movie_number="ABP-001",
        title="JavDB标题",
        summary="JavDB描述",
    )

    with pytest.raises(ValueError, match="不支持的字段"):
        CatalogImportService().update_movie_fields(detail, ("heat",))

    with pytest.raises(ValueError, match="fields 不能为空"):
        CatalogImportService().update_movie_fields(detail, ())
