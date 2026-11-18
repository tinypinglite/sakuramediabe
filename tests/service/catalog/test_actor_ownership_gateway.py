from src.model import Actor
from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway


def test_update_host_source_gender(test_db):
    actor = Actor.create(name="gateway", gender=1)

    assert ActorOwnershipGateway.update_host_source(actor.id, {"gender": 2})

    stored = Actor.get_by_id(actor.id)
    assert stored.gender == 2


def test_patch_plugin_gender(test_db):
    actor = Actor.create(name="gateway-plugin", gender=1)

    assert ActorOwnershipGateway.patch_plugin(
        actor.id, "demo", {"gender": 2}, actor.mutation_revision
    )

    stored = Actor.get_by_id(actor.id)
    assert stored.gender == 2
