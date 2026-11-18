from types import SimpleNamespace

from src.model import Actor
from src.plugins.extensions.metadata import PluginMetadataActor
from src.service.catalog.actor_resolution_service import (
    ActorLookupIndex,
    ActorResolutionService,
)


def test_find_existing_prefers_primary_name_then_lowest_id(test_db):
    Actor.create(name="別名俳優", alias_name="鬼头桃菜")
    primary = Actor.create(name="鬼头桃菜")

    assert ActorResolutionService.find_existing(["鬼头桃菜"]).id == primary.id

    duplicate = Actor.create(name="另一个别名", alias_name="鬼头桃菜")
    assert ActorResolutionService.find_existing(["鬼头桃菜"]).id == primary.id
    assert ActorResolutionService.find_existing(["另一个别名"]).id == duplicate.id

    second_alias = Actor.create(name="第二别名", alias_name="重叠别名")
    first_alias = Actor.create(name="第一别名", alias_name="重叠别名")
    assert ActorResolutionService.find_existing(["重叠别名"]).id == min(
        first_alias.id, second_alias.id
    )


def test_resolve_movie_actors_creates_local_actor_and_aliases(test_db, monkeypatch):
    monkeypatch.setattr(
        ActorResolutionService, "ensure_avatar", classmethod(lambda *_args, **_kwargs: None)
    )

    actors = ActorResolutionService.resolve_movie_actors(
        [PluginMetadataActor(name="新演员", alias_names=["别名A", "别名B"])]
    )

    assert len(actors) == 1
    stored = Actor.get_by_id(actors[0].id)
    assert stored.name == "新演员"
    assert "别名A" in stored.alias_name
    assert "别名B" in stored.alias_name


def test_resolve_movie_actors_applies_gender_without_overwriting_manual(
    test_db, monkeypatch
):
    monkeypatch.setattr(
        ActorResolutionService, "ensure_avatar", classmethod(lambda *_args, **_kwargs: None)
    )
    unknown = Actor.create(name="未知演员", gender=0)
    manual = Actor.create(
        name="人工演员",
        gender=0,
        field_owners={"gender": "host:manual"},
    )
    known = Actor.create(name="已知演员", gender=2)

    ActorResolutionService.resolve_movie_actors(
        [
            PluginMetadataActor(name="未知演员", gender=1),
            PluginMetadataActor(name="人工演员", gender=1),
            PluginMetadataActor(name="已知演员", gender=1),
            PluginMetadataActor(name="新演员", gender=1),
        ]
    )

    filled = Actor.get_by_id(unknown.id)
    assert filled.gender == 1
    assert filled.field_owners["gender"] == "host:metadata"
    assert Actor.get_by_id(manual.id).gender == 0
    assert Actor.get_by_id(manual.id).field_owners["gender"] == "host:manual"
    assert Actor.get_by_id(known.id).gender == 2
    assert Actor.get(Actor.name == "新演员").gender == 1


def test_actor_lookup_index_is_loaded_once_and_tracks_new_actors(
    test_db, monkeypatch
):
    monkeypatch.setattr(
        ActorResolutionService, "ensure_avatar", classmethod(lambda *_args, **_kwargs: None)
    )
    load_count = 0
    original = ActorLookupIndex._ensure_loaded_locked

    def counting_load(index):
        nonlocal load_count
        if not index._loaded:
            load_count += 1
        original(index)

    monkeypatch.setattr(
        ActorLookupIndex, "_ensure_loaded_locked", counting_load
    )
    lookup = ActorLookupIndex()

    ActorResolutionService.resolve_movie_actors(
        [PluginMetadataActor(name="缓存演员")],
        lookup=lookup,
    )
    ActorResolutionService.resolve_movie_actors(
        [PluginMetadataActor(name="缓存演员")],
        lookup=lookup,
    )

    assert load_count == 1
    assert Actor.select().where(Actor.name == "缓存演员").count() == 1


def test_actor_lookup_index_tracks_alias_updates(test_db):
    actor = Actor.create(name="演员")
    lookup = ActorLookupIndex.load()

    actor.alias_name = "演员 / 新别名"
    actor.save(only=[Actor.alias_name])
    lookup.add_or_refresh(actor)

    assert lookup.find_existing(["新别名"]).id == actor.id


def test_actor_lookup_index_normalizes_primary_and_alias_names(test_db):
    actor = Actor.create(
        name="Ｍｉｋａｍｉ Ｙｕａ",
        alias_name="Ｍｉｋａｍｉ Ｙｕａ / 鬼头 桃菜",
    )
    lookup = ActorLookupIndex.load()

    assert lookup.find_existing(["mikami yua"]).id == actor.id
    assert lookup.find_existing(["鬼头桃菜"]).id == actor.id


def test_import_gfriends_candidate_reuses_existing_actor(test_db, monkeypatch):
    existing = Actor.create(name="三上悠亚", alias_name="三上悠亞")
    candidate = SimpleNamespace(
        name="三上悠亜",
        alias_names=("三上悠亚", "鬼头桃菜"),
    )

    class Directory:
        def candidate(self, candidate_id):
            assert candidate_id == "candidate-1"
            return candidate

    monkeypatch.setattr(
        ActorResolutionService, "ensure_avatar", classmethod(lambda *_args, **_kwargs: None)
    )

    actor = ActorResolutionService.import_gfriends_candidate(
        "candidate-1", directory=Directory()
    )

    assert actor.id == existing.id
    stored = Actor.get_by_id(actor.id)
    assert "鬼头桃菜" in stored.alias_name
