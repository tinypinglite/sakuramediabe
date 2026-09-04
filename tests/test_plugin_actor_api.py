"""演员插件公开契约：归属、原子冲突、空值、宿主并发写与迁移。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from importlib import import_module
from threading import Barrier

import pytest
from click.testing import CliRunner

from src.metadata._providers.models import JavdbMovieActorResource
from src.model import Actor
from src.model.catalog.actors import PROTECTED_ACTOR_FIELDS
from src.plugins import PluginContext
from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway
from src.service.catalog.actor_service import ActorService
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.subscribed_actor_movie_sync_service import (
    SubscribedActorMovieSyncService,
)


@pytest.fixture
def actors(test_db, tmp_path):
    return PluginContext("actor_demo", {}, tmp_path).actors


def test_actor_snapshot_and_cursor_pages(actors):
    rows = [Actor.create(javdb_id=f"actor-{i}", name=f"演员{i}") for i in range(3)]
    page = actors.list_page(limit=2)
    assert [item.actor_id for item in page.items] == [row.id for row in rows[:2]]
    assert page.next_cursor == rows[1].id
    last = actors.list_page(after_id=page.next_cursor, limit=2)
    assert [item.actor_id for item in last.items] == [rows[2].id]
    assert last.next_cursor is None
    assert actors.list_page(after_id=rows[-1].id).items == ()
    snapshot = actors.get(rows[0].id)
    assert snapshot.values["javdb_id"] == "actor-0"
    assert actors.get(999999) is None
    with pytest.raises(TypeError):
        snapshot.values["name"] = "不能修改"


def test_patch_ownership_conflict_null_and_release(actors, tmp_path):
    actor = Actor.create(javdb_id="actor-1", name="演员")
    other = PluginContext("other", {}, tmp_path).actors
    assert actors.patch(actor.id, {"birthday": "1993-08-16", "height_cm": 159}, 0)
    snapshot = actors.get(actor.id)
    assert snapshot.values["birthday"] == date(1993, 8, 16)
    assert snapshot.owners == {"birthday": "plugin:actor_demo", "height_cm": "plugin:actor_demo"}
    assert not actors.patch(actor.id, {"cup": "F"}, 0)
    assert not other.patch(actor.id, {"cup": "F", "height_cm": 160}, 1)
    assert actors.get(actor.id).values["cup"] is None
    assert other.patch(actor.id, {"cup": "F"}, 1)
    assert actors.patch(actor.id, {"birthday": None}, 2)
    assert actors.get(actor.id).owners["birthday"] == "plugin:actor_demo"
    assert ActorOwnershipGateway.release_plugin_owners("actor_demo", ("birthday", "cup")) == 1
    snapshot = actors.get(actor.id)
    assert snapshot.revision == 4
    assert snapshot.owners == {"height_cm": "plugin:actor_demo", "cup": "plugin:other"}
    assert snapshot.values["height_cm"] == 159
    assert not actors.patch(actor.id, {"birthday": "1993-08-16"}, 3)
    assert ActorOwnershipGateway.release_plugin_owners("actor_demo") == 1
    assert ActorOwnershipGateway.release_plugin_owners("actor_demo") == 0
    assert actors.get(actor.id).owners == {"cup": "plugin:other"}
    assert not actors.patch(999999, {"cup": "F"}, 0)


@pytest.mark.parametrize("fields", [
    {"height_cm": 160, "is_subscribed": True},
    {"height_cm": 160, "blood_type": 5},
])
def test_invalid_patch_is_rejected_without_changes(actors, fields):
    actor = Actor.create(javdb_id="actor-1", name="演员")
    with pytest.raises(ValueError):
        actors.patch(actor.id, fields, 0)
    assert actors.get(actor.id).revision == 0
    assert actors.get(actor.id).values["height_cm"] is None


def test_concurrent_patches_have_one_winner(actors, test_db, tmp_path):
    actor = Actor.create(javdb_id="actor-1", name="演员")
    barrier = Barrier(2)

    def patch(plugin_id):
        with test_db.connection_context():
            api = PluginContext(plugin_id, {}, tmp_path).actors
            barrier.wait(timeout=10)
            return api.patch(actor.id, {"height_cm": 160}, 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(patch, ("first", "second")))
    assert sorted(results) == [False, True]
    assert actors.get(actor.id).revision == 1


def test_host_writes_preserve_plugin_metadata(actors):
    actor = Actor.create(javdb_id="actor-1", name="旧名字")
    # 订阅同步在 HTTP 请求前已读入旧 ORM 对象，模拟期间插件完成更新。
    class Provider:
        def get_actor_movies_by_javdb(self, **kwargs):
            assert actors.patch(actor.id, {"height_cm": 159}, 0)
            return []

    SubscribedActorMovieSyncService(provider=Provider())._sync_actor(actor)
    resource = JavdbMovieActorResource(javdb_id="actor-1", name="新名字", gender=1)
    importer = CatalogImportService()
    importer.upsert_actor_from_javdb_resource(resource, update_gender=True)
    importer._refresh_actor_from_javdb_resource_strict(actor_resource=resource, profile_image_task=None)
    ActorService.set_subscription(actor.id, True)
    snapshot = actors.get(actor.id)
    assert snapshot.values["height_cm"] == 159
    assert snapshot.values["name"] == "新名字"
    assert snapshot.values["gender"] == 1
    assert snapshot.values["is_subscribed"] is True
    assert snapshot.revision == 1
    with pytest.raises(RuntimeError):
        actor.save()
    with pytest.raises(RuntimeError):
        Actor.update(field_owners={})


def test_detail_exposes_profile_and_computes_age(actors, monkeypatch, client, account_user):
    actor = Actor.create(javdb_id="actor-1", name="演员")
    fields = {
        "birthday": "1993-08-16", "height_cm": 159, "bust_cm": 84,
        "waist_cm": 58, "hips_cm": 88, "cup": "F", "birthplace": "出生地", "blood_type": "O",
    }
    assert actors.patch(actor.id, fields, 0)
    monkeypatch.setattr("src.model.catalog.actors.utc_now_for_db", lambda: datetime(2026, 8, 16))
    token = client.post("/auth/tokens", json={
        "username": account_user.username, "password": "password123",
    }).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(f"/actors/{actor.id}", headers=headers)
    assert response.status_code == 200
    assert {key: response.json()[key] for key in fields} == fields
    assert response.json()["age"] == 33
    listing = client.get("/actors", headers=headers)
    assert listing.status_code == 200
    assert "birthday" not in listing.json()["items"][0]


def test_actor_migration_preserves_rows_and_is_idempotent(clean_db):
    clean_db.execute_sql("CREATE TABLE actor (id SERIAL PRIMARY KEY, name TEXT NOT NULL)")
    clean_db.execute_sql("INSERT INTO actor (name) VALUES ('existing')")
    migration = import_module("src.start.migrations.versions.20260905_01_add_actor_metadata")
    migration.migrate(clean_db)
    migration.migrate(clean_db)
    row = clean_db.execute_sql("SELECT name, birthday, field_owners, mutation_revision FROM actor").fetchone()
    assert row == ("existing", None, {}, 0)
    assert PROTECTED_ACTOR_FIELDS <= {column.name for column in clean_db.get_columns("actor")}


def test_cli_releases_actor_ownership(actors, monkeypatch):
    from src.start.commands import main

    actor = Actor.create(javdb_id="actor-1", name="演员")
    assert actors.patch(actor.id, {"height_cm": 159}, 0)
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    result = CliRunner().invoke(main, [
        "plugins", "clear-field-owners", "--entity", "actor", "--plugin-id", "actor_demo",
        "--field", "height_cm",
    ])
    assert result.exit_code == 0, result.output
    assert actors.get(actor.id).owners == {}
    assert actors.get(actor.id).values["height_cm"] == 159
