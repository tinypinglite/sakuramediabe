"""MovieOwnershipGateway 与 save/update 护栏回归测试（v2-lite）。

覆盖文档验收的最低保证：字段 owner 与值同语句提交、revision 乐观并发、
宿主窄更新不覆盖已接管字段、save/update 运行时护栏。白名单当前真实开放
title / summary / is_collection，测试直接跑真实配置。
"""

from __future__ import annotations

import pytest

from src.model import Movie
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway


def _create_movie(test_db, **overrides) -> Movie:
    fields = {
        "javdb_id": "javdb-1",
        "movie_number": "ABP-001",
        "title": "旧标题",
        "summary": "旧简介",
    }
    fields.update(overrides)
    return Movie.create(**fields)


def test_patch_plugin_takes_ownership_and_increments_revision(test_db):
    movie = _create_movie(test_db)

    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "ranking", {"title": "插件标题"}, expected_revision=0
    ) is True

    movie = Movie.get_by_id(movie.id)
    assert movie.title == "插件标题"
    assert movie.mutation_revision == 1
    assert movie.field_owners == {"title": "plugin:ranking"}


def test_patch_plugin_stale_revision_rejected(test_db):
    """两个插件/两次写基于同一旧 revision 时，后到者整次零修改（乐观并发）。"""
    movie = _create_movie(test_db)
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A 标题"}, expected_revision=0
    ) is True

    # 同 revision 的第二次 patch：字段已有 owner 且不是自己 + revision 已变，双条件失败。
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-b", {"title": "B 标题"}, expected_revision=0
    ) is False

    movie = Movie.get_by_id(movie.id)
    assert movie.title == "A 标题"
    assert movie.mutation_revision == 1
    assert movie.field_owners == {"title": "plugin:plugin-a"}


def test_patch_plugin_owner_cannot_steal_other_plugin_field(test_db):
    movie = _create_movie(test_db)
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A 标题"}, expected_revision=0
    ) is True

    # 拿到最新 revision 的第三方也无法接管：owner 不是自己。
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-b", {"title": "B 标题"}, expected_revision=1
    ) is False

    movie = Movie.get_by_id(movie.id)
    assert movie.title == "A 标题"
    assert movie.field_owners == {"title": "plugin:plugin-a"}


def test_patch_plugin_owner_can_continue_with_new_revision(test_db):
    movie = _create_movie(test_db)
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "第一版"}, expected_revision=0
    ) is True
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "第二版"}, expected_revision=1
    ) is True

    movie = Movie.get_by_id(movie.id)
    assert movie.title == "第二版"
    assert movie.mutation_revision == 2


def test_patch_plugin_rejects_non_protected_field(test_db):
    movie = _create_movie(test_db)
    with pytest.raises(ValueError, match="非受保护字段"):
        MovieOwnershipGateway.patch_plugin(
            movie.id, "plugin-a", {"heat": 999}, expected_revision=0
        )


def test_patch_plugin_rejects_wrong_value_type(test_db):
    """codec 校验：title/summary 只接受 str，非字符串值拒绝落库。"""
    movie = _create_movie(test_db)
    with pytest.raises(ValueError, match="值类型错误"):
        MovieOwnershipGateway.patch_plugin(
            movie.id, "plugin-a", {"title": 12345}, expected_revision=0
        )
    with pytest.raises(ValueError, match="值类型错误"):
        MovieOwnershipGateway.patch_plugin(
            movie.id, "plugin-a", {"title": None}, expected_revision=0
        )


def test_patch_plugin_can_take_collection_ownership(test_db):
    movie = _create_movie(test_db)

    assert MovieOwnershipGateway.patch_plugin(
        movie.id,
        "collection-plugin",
        {"is_collection": True},
        expected_revision=0,
    ) is True

    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is True
    assert movie.field_owners == {"is_collection": "plugin:collection-plugin"}
    assert movie.mutation_revision == 1


def test_update_host_unowned_skips_owned_field(test_db):
    movie = _create_movie(test_db)
    assert MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "插件标题"}, expected_revision=0
    ) is True

    # 宿主刷新：title 已被接管，不覆盖；summary 未接管，正常更新。
    affected = MovieOwnershipGateway.update_host_unowned(
        movie.id,
        {"title": "宿主标题", "summary": "宿主简介"},
    )
    assert affected == 1

    movie = Movie.get_by_id(movie.id)
    assert movie.title == "插件标题"
    assert movie.summary == "宿主简介"
    # 只 summary 变化：revision 只 +1。
    assert movie.mutation_revision == 2


def test_update_host_unowned_unchanged_values_keep_revision(test_db):
    """NULL-safe 变化检测：值未变时 revision 与 updated_at 都不动。"""
    movie = _create_movie(test_db)

    affected = MovieOwnershipGateway.update_host_unowned(
        movie.id,
        {"title": "旧标题", "summary": "旧简介"},
    )
    assert affected == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.mutation_revision == 0

    # 值变化才递增。
    MovieOwnershipGateway.update_host_unowned(movie.id, {"summary": "新简介"})
    movie = Movie.get_by_id(movie.id)
    assert movie.mutation_revision == 1


def test_update_host_unowned_accepts_none_for_nullable_fields(test_db):
    """宿主写路径放行 None：远端缺失厂商/导演时以 NULL 落库（列可空，SQL 已 NULL-safe）。

    插件 patch 路径仍拒绝 None（见 test_patch_plugin_rejects_wrong_value_type）。
    """
    movie = _create_movie(test_db, maker_name="旧厂商", director_name="旧导演")

    affected = MovieOwnershipGateway.update_host_unowned(
        movie.id,
        {"maker_name": None, "director_name": None},
    )
    assert affected == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.maker_name is None
    assert movie.director_name is None
    # 两个字段都从非空变为 NULL：各算一次变化，revision +2。
    assert movie.mutation_revision == 2

    # 未接管字段从 None 再写 None：值未变，revision 不动。
    MovieOwnershipGateway.update_host_unowned(
        movie.id,
        {"maker_name": None, "director_name": None},
    )
    movie = Movie.get_by_id(movie.id)
    assert movie.mutation_revision == 2


def test_update_host_manual_overrides_plugin_and_blocks_auto_updates(test_db):
    movie = _create_movie(test_db)
    assert MovieOwnershipGateway.patch_plugin(
        movie.id,
        "collection-plugin",
        {"is_collection": True},
        expected_revision=0,
    ) is True

    assert MovieOwnershipGateway.update_host_manual(
        [movie.id], {"is_collection": False}
    ) == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is False
    assert movie.field_owners == {"is_collection": "host:manual"}
    assert movie.mutation_revision == 2

    MovieOwnershipGateway.update_host_unowned(movie.id, {"is_collection": True})
    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is False
    assert movie.mutation_revision == 2


def test_save_guard_rejects_full_save(test_db):
    movie = _create_movie(test_db)
    movie.title = "直接改"
    with pytest.raises(RuntimeError, match="必须传 only"):
        movie.save()


def test_save_guard_rejects_only_with_protected_field(test_db):
    movie = _create_movie(test_db)
    movie.title = "直接改"
    with pytest.raises(RuntimeError, match="受保护字段禁止直接写入"):
        movie.save(only=[Movie.title])

    # 非保护字段窄更新放行。
    movie.heat = 123
    movie.save(only=[Movie.heat])
    movie = Movie.get_by_id(movie.id)
    assert movie.heat == 123

    movie.is_collection = True
    with pytest.raises(RuntimeError, match="受保护字段禁止直接写入"):
        movie.save(only=[Movie.is_collection])


def test_save_guard_rejects_string_only_with_protected_field(test_db):
    """peewee 的 save(only=...) 接受字段名字符串，护栏同样拦截。"""
    movie = _create_movie(test_db)
    movie.title = "直接改"
    with pytest.raises(RuntimeError, match="受保护字段禁止直接写入"):
        movie.save(only=["title"])

    # 非保护字段字符串窄更新放行。
    movie.heat = 123
    movie.save(only=["heat"])
    movie = Movie.get_by_id(movie.id)
    assert movie.heat == 123

    movie.is_collection = True
    with pytest.raises(RuntimeError, match="受保护字段禁止直接写入"):
        movie.save(only=["is_collection"])


def test_update_guard_rejects_protected_fields(test_db):
    movie = _create_movie(test_db)
    with pytest.raises(RuntimeError, match="禁止直接 UPDATE"):
        Movie.update(title="批量改").where(Movie.id == movie.id).execute()
    with pytest.raises(RuntimeError, match="禁止直接 UPDATE"):
        Movie.update({Movie.title: "批量改"}).where(Movie.id == movie.id).execute()

    with pytest.raises(RuntimeError, match="禁止直接 UPDATE"):
        Movie.update({Movie.is_collection: True}).where(Movie.id == movie.id).execute()

    # 非保护字段批量更新放行。
    Movie.update({Movie.heat: 123}).where(Movie.id == movie.id).execute()
    movie = Movie.get_by_id(movie.id)
    assert movie.heat == 123


def test_update_guard_rejects_mixed_dict_and_kwargs(test_db):
    """peewee 的 __data dict 与 kwargs 合并更新，两侧都必须检查，杜绝绕过。"""
    movie = _create_movie(test_db)
    with pytest.raises(RuntimeError, match="禁止直接 UPDATE"):
        Movie.update({Movie.is_collection: True}, title="混合改").where(
            Movie.id == movie.id
        ).execute()
    movie = Movie.get_by_id(movie.id)
    assert movie.title == "旧标题"
    assert movie.is_collection is False


def test_release_plugin_owners_all_fields(test_db):
    movie = _create_movie(test_db)
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A"}, expected_revision=0
    )
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"summary": "B"}, expected_revision=1
    )

    assert MovieOwnershipGateway.release_plugin_owners("plugin-a") == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.field_owners == {}
    assert movie.title == "A"
    assert movie.mutation_revision == 2


def test_release_plugin_owners_specific_field(test_db):
    movie = _create_movie(test_db)
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A"}, expected_revision=0
    )
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-b", {"summary": "B"}, expected_revision=1
    )

    assert MovieOwnershipGateway.release_plugin_owners("plugin-a", ("title",)) == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.field_owners == {"summary": "plugin:plugin-b"}
    assert movie.title == "A"

    # 不存在的 owner / 字段组合不影响其他记录。
    assert MovieOwnershipGateway.release_plugin_owners("plugin-a", ("summary",)) == 0


def test_release_plugin_owners_specific_field_does_not_steal_other_owner(test_db):
    """指定字段时只摘除属于目标插件的 key，其他插件接管的字段必须保留。"""
    movie = _create_movie(test_db)
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A"}, expected_revision=0
    )
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-b", {"summary": "B"}, expected_revision=1
    )
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-c", {"maker_name": "C 社"}, expected_revision=2
    )

    # 列表里混入属于其他插件的字段：只应摘除 plugin-a 的 title，summary/maker_name 保留。
    assert MovieOwnershipGateway.release_plugin_owners(
        "plugin-a", ("title", "summary", "maker_name")
    ) == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.field_owners == {
        "summary": "plugin:plugin-b",
        "maker_name": "plugin:plugin-c",
    }


def test_release_plugin_owners_specific_field_no_match_keeps_owners(test_db):
    """目标插件不持有任何列出的字段时零修改，其他 owner 不动。"""
    movie = _create_movie(test_db)
    MovieOwnershipGateway.patch_plugin(
        movie.id, "plugin-a", {"title": "A"}, expected_revision=0
    )

    assert MovieOwnershipGateway.release_plugin_owners("plugin-a", ("summary",)) == 0
    movie = Movie.get_by_id(movie.id)
    assert movie.field_owners == {"title": "plugin:plugin-a"}
