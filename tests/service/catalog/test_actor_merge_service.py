from datetime import date, datetime

import pytest

from src.api.exception.errors import ApiError
from src.model import Actor, Image, Movie, MovieActor
from src.service.catalog import ActorMergeService, ActorService, MovieService
from src.service.system.status_service import StatusService


def _create_movie(number: str) -> Movie:
    return Movie.create(
        movie_number=number,
        javdb_id=f"javdb-{number}",
        title=number,
        release_date=datetime(2024, 5, 1),
    )


def _create_image(path: str) -> Image:
    return Image.create(origin=path, small=path, medium=path, large=path)


def test_merge_moves_movie_links_and_dedupes_shared_movies(test_db):
    target = Actor.create(javdb_id="merge-target", name="三上悠亚")
    source = Actor.create(javdb_id="merge-source", name="鬼头桃菜")
    shared = _create_movie("MERGE-001")
    only_source = _create_movie("MERGE-002")
    MovieActor.create(movie=shared, actor=target)
    MovieActor.create(movie=shared, actor=source)
    MovieActor.create(movie=only_source, actor=source)

    detail = ActorMergeService.merge_actors(target.id, [source.id])

    assert detail.id == target.id
    assert detail.movie_count == 2
    assert {
        link.movie_id
        for link in MovieActor.select().where(MovieActor.actor == target.id)
    } == {shared.id, only_source.id}
    assert MovieActor.select().where(MovieActor.actor == source.id).count() == 0
    tombstone = Actor.get_by_id(source.id)
    assert tombstone.merged_into_id == target.id
    assert tombstone.is_subscribed is False


def test_merge_combines_alias_subscription_and_resets_sync_marker(test_db):
    earlier = datetime(2024, 1, 1, 8, 0, 0)
    later = datetime(2024, 6, 1, 8, 0, 0)
    target = Actor.create(
        javdb_id="merge-target",
        name="三上悠亚",
        alias_name="三上悠亞",
        subscribed_movies_full_synced_at=later,
    )
    source = Actor.create(
        javdb_id="merge-source",
        name="鬼头桃菜",
        alias_name="鬼頭桃菜 / きとうももな",
        display_name_override="桃子",
        is_subscribed=True,
        subscribed_at=earlier,
    )

    ActorMergeService.merge_actors(target.id, [source.id])

    merged = Actor.get_by_id(target.id)
    assert merged.is_subscribed is True
    assert merged.subscribed_at == earlier
    assert merged.subscribed_movies_full_synced_at is None
    assert merged.alias_name.split(" / ")[0] == "三上悠亚"
    for name in ("三上悠亞", "鬼头桃菜", "鬼頭桃菜", "きとうももな", "桃子"):
        assert name in merged.alias_name
    tombstone = Actor.get_by_id(source.id)
    assert tombstone.is_subscribed is False
    assert tombstone.subscribed_at is None


def test_merge_gap_fills_profile_and_skips_manual_source_fields(test_db):
    target = Actor.create(javdb_id="merge-target", name="保留", gender=0)
    source = Actor.create(
        javdb_id="merge-source",
        name="来源",
        birthday=date(1998, 4, 12),
        height_cm=160,
        cup="F",
        field_owners={"birthday": "host:manual", "height_cm": "host:javdb"},
    )

    ActorMergeService.merge_actors(target.id, [source.id])

    merged = Actor.get_by_id(target.id)
    assert merged.birthday is None
    assert merged.height_cm == 160
    assert merged.cup == "F"
    assert merged.field_owners.get("height_cm") == "host:javdb"
    assert merged.mutation_revision == 1


def test_merge_takes_over_source_image_only_when_target_has_none(test_db):
    source_image = _create_image("actors/merge-source.webp")
    target_with_image = _create_image("actors/merge-target.webp")

    target = Actor.create(javdb_id="merge-a", name="无头像")
    source = Actor.create(
        javdb_id="merge-b", name="有头像", profile_image_override=source_image
    )
    ActorMergeService.merge_actors(target.id, [source.id])

    merged = Actor.get_by_id(target.id)
    assert merged.profile_image_override_id == source_image.id
    assert Actor.get_by_id(source.id).profile_image_override_id is None

    target_full = Actor.create(
        javdb_id="merge-c", name="已带头像", profile_image=target_with_image
    )
    source_skip = Actor.create(
        javdb_id="merge-d", name="来源头像", profile_image_override=source_image
    )
    ActorMergeService.merge_actors(target_full.id, [source_skip.id])

    assert Actor.get_by_id(target_full.id).profile_image_override_id is None
    assert Actor.get_by_id(source_skip.id).profile_image_override_id == source_image.id


def test_merge_cascades_existing_tombstones(test_db):
    first = Actor.create(javdb_id="merge-1", name="第一")
    second = Actor.create(javdb_id="merge-2", name="第二")
    third = Actor.create(javdb_id="merge-3", name="第三")
    ActorMergeService.merge_actors(first.id, [second.id])

    ActorMergeService.merge_actors(third.id, [first.id])

    assert Actor.get_by_id(first.id).merged_into_id == third.id
    assert Actor.get_by_id(second.id).merged_into_id == third.id


def test_merge_validation_and_idempotency(test_db):
    target = Actor.create(javdb_id="merge-target", name="保留")
    other = Actor.create(javdb_id="merge-other", name="来源")

    with pytest.raises(ApiError) as self_error:
        ActorMergeService.merge_actors(target.id, [target.id])
    assert self_error.value.code == "invalid_actor_merge"
    assert self_error.value.details["reason"] == "merge_self"

    with pytest.raises(ApiError) as missing_error:
        ActorMergeService.merge_actors(target.id, [999999])
    assert missing_error.value.status_code == 404

    ActorMergeService.merge_actors(target.id, [other.id])
    again = ActorMergeService.merge_actors(target.id, [other.id])
    assert again.id == target.id

    third = Actor.create(javdb_id="merge-third", name="另一个保留")
    with pytest.raises(ApiError) as merged_error:
        ActorMergeService.merge_actors(third.id, [other.id])
    assert merged_error.value.details["reason"] == "source_already_merged"


def test_tombstone_hidden_and_resolved_across_queries(test_db):
    target = Actor.create(javdb_id="merge-target", name="新名", gender=1)
    source = Actor.create(javdb_id="merge-source", name="旧名", gender=1)
    movie = _create_movie("MERGE-READ-1")
    MovieActor.create(movie=movie, actor=source)

    ActorMergeService.merge_actors(target.id, [source.id])

    page = ActorService.list_actors(query="旧名", page=1, page_size=10)
    assert [(item.id, item.name) for item in page.items] == [(target.id, "新名")]
    assert ActorService.list_actors().total == 1
    assert ActorService.get_filter_options().actor_count == 1

    assert ActorService.get_actor_detail(source.id).id == target.id
    assert ActorService.get_actor_movie_ids(source.id) == [movie.id]
    years = ActorService.get_actor_years(source.id)
    assert [(item.year, item.movie_count) for item in years] == [(2024, 1)]

    movies = MovieService.list_movies(actor_id=source.id)
    assert [item.id for item in movies.items] == [movie.id]

    ActorService.set_subscription(source.id, True)
    assert Actor.get_by_id(target.id).is_subscribed is True

    status = StatusService.get_status()
    assert status.actors.female_total == 1
    assert status.actors.female_subscribed == 1
