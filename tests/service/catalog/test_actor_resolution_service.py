from types import SimpleNamespace

from src.model import Actor
from src.plugins.extensions.metadata import PluginMetadataActor
from src.service.catalog.actor_resolution_service import ActorResolutionService


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
