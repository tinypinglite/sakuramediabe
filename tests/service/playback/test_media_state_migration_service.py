from datetime import datetime, timedelta
from types import SimpleNamespace

from src.config.config import settings
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
    Movie,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback import media_service
from src.service.playback.media_service import MediaService
from src.service.playback.media_state_migration_service import (
    MediaStateMigrationService,
)


def _movie(number: str = "STATE-001") -> Movie:
    return Movie.create(movie_number=number, javdb_id=number.lower(), title=number)


def _library(name: str, provider_key: str = "demo") -> MediaLibrary:
    return MediaLibrary.create(
        name=name,
        provider_key=provider_key,
        provider_config={},
    )


def _media(
    library: MediaLibrary,
    movie: Movie,
    *,
    file_name: str,
    bit_rate: int | None = None,
    resolution: str = "1920x1080",
    file_size_bytes: int = 1_000,
    duration_seconds: int = 100,
    valid: bool = True,
    created_at: datetime | None = None,
) -> Media:
    video_info = {"video": {"bit_rate": bit_rate}} if bit_rate is not None else None
    values = {
        "movie": movie,
        "library": library,
        "file_name": file_name,
        "resolution": resolution,
        "file_size_bytes": file_size_bytes,
        "duration_seconds": duration_seconds,
        "video_info": video_info,
        "valid": valid,
    }
    if created_at is not None:
        values["created_at"] = created_at
    return Media.create(**values)


def _patch_delete_dependencies(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: SimpleNamespace(delete_media=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        media_service,
        "get_qdrant_thumbnail_store",
        lambda: SimpleNamespace(delete_by_media_id=lambda _media_id: None),
    )


def test_resolve_target_prefers_bitrate_then_resolution_size_and_local(
    test_db,
):
    movie = _movie()
    cloud = _library("cloud")
    local = _library("local", provider_key="local")
    source = _media(cloud, movie, file_name="source.mp4", bit_rate=1)
    _media(cloud, movie, file_name="low.mp4", bit_rate=1_000, resolution="1280x720")
    _media(cloud, movie, file_name="high.mp4", bit_rate=2_000, resolution="1280x720")
    _media(cloud, movie, file_name="hd.mp4", bit_rate=2_000, resolution="1920x1080")
    _media(
        cloud,
        movie,
        file_name="large.mp4",
        bit_rate=2_000,
        resolution="1920x1080",
        file_size_bytes=2_000,
    )
    expected = _media(
        local,
        movie,
        file_name="local.mp4",
        bit_rate=2_000,
        resolution="1920x1080",
        file_size_bytes=2_000,
    )
    _media(
        cloud,
        movie,
        file_name="invalid.mp4",
        bit_rate=9_000,
        valid=False,
    )

    assert MediaStateMigrationService.resolve_target_media(source).id == expected.id
    assert (
        MediaStateMigrationService.resolve_target_media(
            source,
            excluded_media_ids=frozenset({expected.id}),
        ).file_name
        == "large.mp4"
    )


def test_resolve_target_estimates_missing_bitrate_from_size_and_duration(
    test_db,
):
    movie = _movie("STATE-002")
    library = _library("estimate")
    source = _media(library, movie, file_name="source.mp4")
    _media(
        library,
        movie,
        file_name="measured.mp4",
        bit_rate=1_000,
        resolution="1280x720",
    )
    expected = _media(
        library,
        movie,
        file_name="estimated.mp4",
        resolution="1920x1080",
        file_size_bytes=20_000,
        duration_seconds=10,
    )

    assert MediaStateMigrationService.resolve_target_media(source).id == expected.id


def test_migrate_progress_creates_target_progress_and_maps_duration(
    test_db,
):
    movie = _movie("STATE-003")
    library = _library("progress")
    source = _media(
        library,
        movie,
        file_name="source.mp4",
        duration_seconds=200,
    )
    target = _media(
        library,
        movie,
        file_name="target.mp4",
        duration_seconds=100,
    )
    watched_at = datetime(2026, 1, 1, 12, 0, 0)
    source_progress = MediaProgress.create(
        media=source,
        position_seconds=50,
        last_watched_at=watched_at,
    )

    result = MediaStateMigrationService.migrate(source, target)

    target_progress = MediaProgress.get(MediaProgress.media == target.id)
    assert result.progress_migrated is True
    assert target_progress.position_seconds == 25
    assert target_progress.last_watched_at == watched_at
    assert target_progress.created_at == source_progress.created_at


def test_migrate_progress_keeps_newer_target_and_updates_older_target(
    test_db,
):
    movie = _movie("STATE-004")
    library = _library("progress-newer")
    source = _media(library, movie, file_name="source.mp4")
    target = _media(library, movie, file_name="target.mp4")
    older = datetime(2026, 1, 1, 12, 0, 0)
    newer = older + timedelta(hours=1)
    MediaProgress.create(
        media=source,
        position_seconds=40,
        last_watched_at=older,
    )
    MediaProgress.create(
        media=target,
        position_seconds=70,
        last_watched_at=newer,
    )

    result = MediaStateMigrationService.migrate(source, target)
    assert result.progress_migrated is False
    assert MediaProgress.get(MediaProgress.media == target.id).position_seconds == 70

    MediaProgress.update(last_watched_at=older - timedelta(hours=1)).where(
        MediaProgress.media == target.id
    ).execute()
    result = MediaStateMigrationService.migrate(source, target)
    assert result.progress_migrated is True
    assert MediaProgress.get(MediaProgress.media == target.id).position_seconds == 40


def test_migrate_progress_ignores_zero_position(test_db):
    movie = _movie("STATE-005")
    library = _library("progress-zero")
    source = _media(library, movie, file_name="source.mp4")
    target = _media(library, movie, file_name="target.mp4")
    MediaProgress.create(
        media=source,
        position_seconds=0,
        last_watched_at=datetime(2026, 1, 1),
    )

    result = MediaStateMigrationService.migrate(source, target)

    assert result.progress_migrated is False
    assert MediaProgress.get_or_none(MediaProgress.media == target.id) is None


def test_map_progress_position_clamps_to_target_duration():
    assert MediaStateMigrationService._map_progress_position(100, 100, 50) == 50
    assert MediaStateMigrationService._map_progress_position(100, 0, 50) == 50
    assert MediaStateMigrationService._map_progress_position(100, 200, 0) == 100


def test_migrate_points_moves_media_and_links_exact_target_thumbnail(
    test_db,
):
    movie = _movie("STATE-006")
    library = _library("points")
    source = _media(library, movie, file_name="source.mp4")
    target = _media(library, movie, file_name="target.mp4")
    source_thumbnail_image = Image.create(origin="source-thumb.webp")
    source_thumbnail = MediaThumbnail.create(
        media=source,
        image=source_thumbnail_image,
        offset=20,
    )
    target_thumbnail_image = Image.create(origin="target-thumb.webp")
    target_thumbnail = MediaThumbnail.create(
        media=target,
        image=target_thumbnail_image,
        offset=20,
    )
    point_image = Image.create(origin="point.webp")
    point = MediaPoint.create(
        media=source,
        thumbnail=source_thumbnail,
        image=point_image,
        movie_number=movie.movie_number,
        offset_seconds=20,
    )
    collection = MomentCollection.create(name="saved")
    MomentCollectionItem.create(collection=collection, point=point, position=0)

    result = MediaStateMigrationService.migrate(source, target)

    migrated = MediaPoint.get_by_id(point.id)
    assert result.points_migrated == 1
    assert result.points_merged == 0
    assert migrated.media_id == target.id
    assert migrated.thumbnail_id == target_thumbnail.id
    assert migrated.image_id == point_image.id
    assert MomentCollectionItem.get(
        MomentCollectionItem.collection == collection
    ).point_id == point.id


def test_migrate_points_merges_duplicate_offset_and_collection_memberships(
    test_db,
):
    movie = _movie("STATE-007")
    library = _library("points-merge")
    source = _media(library, movie, file_name="source.mp4")
    target = _media(library, movie, file_name="target.mp4")
    source_point_image = Image.create(origin="source-point.webp")
    target_point_image = Image.create(origin="target-point.webp")
    source_point = MediaPoint.create(
        media=source,
        image=source_point_image,
        movie_number=movie.movie_number,
        offset_seconds=30,
    )
    target_point = MediaPoint.create(
        media=target,
        image=target_point_image,
        movie_number=movie.movie_number,
        offset_seconds=30,
    )
    collection = MomentCollection.create(name="merge")
    source_item = MomentCollectionItem.create(
        collection=collection,
        point=source_point,
        position=0,
    )

    result = MediaStateMigrationService.migrate(source, target)

    assert result.points_migrated == 0
    assert result.points_merged == 1
    assert result.obsolete_image_paths == {"source-point.webp"}
    assert MediaPoint.get_or_none(MediaPoint.id == source_point.id) is None
    assert Image.get_or_none(Image.id == source_point_image.id) is None
    assert MomentCollectionItem.get(
        MomentCollectionItem.id == source_item.id
    ).point_id == target_point.id


def test_migrate_points_keeps_out_of_range_point_on_source(test_db):
    movie = _movie("STATE-008")
    library = _library("points-out-of-range")
    source = _media(
        library,
        movie,
        file_name="source.mp4",
        duration_seconds=300,
    )
    target = _media(
        library,
        movie,
        file_name="target.mp4",
        duration_seconds=100,
    )
    point_image = Image.create(origin="out-of-range.webp")
    point = MediaPoint.create(
        media=source,
        image=point_image,
        movie_number=movie.movie_number,
        offset_seconds=200,
    )

    result = MediaStateMigrationService.migrate(source, target)

    assert result.points_migrated == 0
    assert result.points_skipped_out_of_range == 1
    assert MediaPoint.get_by_id(point.id).media_id == source.id


def test_delete_media_migrates_progress_and_points_to_best_remaining_version(
    test_db,
    monkeypatch,
    tmp_path,
):
    _patch_delete_dependencies(monkeypatch, tmp_path)
    movie = _movie("STATE-DELETE-001")
    cloud = _library("delete-cloud")
    local = _library("delete-local", provider_key="local")
    source = _media(
        cloud,
        movie,
        file_name="source.mp4",
        bit_rate=1_000,
    )
    target = _media(
        local,
        movie,
        file_name="target.mp4",
        bit_rate=9_000,
    )
    watched_at = datetime(2026, 1, 2, 12, 0, 0)
    MediaProgress.create(
        media=source,
        position_seconds=40,
        last_watched_at=watched_at,
    )
    target_thumb_image = Image.create(origin="delete-target-thumb.webp")
    target_thumb = MediaThumbnail.create(
        media=target,
        image=target_thumb_image,
        offset=10,
    )
    point_image = Image.create(origin="delete-point.webp")
    point = MediaPoint.create(
        media=source,
        image=point_image,
        movie_number=movie.movie_number,
        offset_seconds=10,
    )

    MediaService.delete_media(source.id)

    assert Media.get_or_none(Media.id == source.id) is None
    target_progress = MediaProgress.get(MediaProgress.media == target.id)
    assert target_progress.position_seconds == 40
    assert target_progress.last_watched_at == watched_at
    migrated_point = MediaPoint.get_by_id(point.id)
    assert migrated_point.media_id == target.id
    assert migrated_point.thumbnail_id == target_thumb.id
    assert migrated_point.image_id == point_image.id


def test_delete_media_excludes_the_whole_batch_from_target_selection(
    test_db,
    monkeypatch,
    tmp_path,
):
    _patch_delete_dependencies(monkeypatch, tmp_path)
    movie = _movie("STATE-DELETE-002")
    library = _library("delete-batch")
    source = _media(library, movie, file_name="source.mp4", bit_rate=1_000)
    also_deleting = _media(
        library,
        movie,
        file_name="also-deleting.mp4",
        bit_rate=20_000,
    )
    survivor = _media(library, movie, file_name="survivor.mp4", bit_rate=5_000)
    MediaProgress.create(
        media=source,
        position_seconds=50,
        last_watched_at=datetime(2026, 1, 3),
    )

    MediaService.delete_media(
        source.id,
        migration_excluded_media_ids=frozenset({source.id, also_deleting.id}),
    )

    assert MediaProgress.get_or_none(
        MediaProgress.media == also_deleting.id
    ) is None
    assert MediaProgress.get(MediaProgress.media == survivor.id).position_seconds == 50


def test_delete_media_does_not_migrate_when_every_version_is_in_the_batch(
    test_db,
    monkeypatch,
    tmp_path,
):
    _patch_delete_dependencies(monkeypatch, tmp_path)
    movie = _movie("STATE-DELETE-003")
    library = _library("delete-all")
    source = _media(library, movie, file_name="source.mp4")
    sibling = _media(library, movie, file_name="sibling.mp4")
    MediaProgress.create(
        media=source,
        position_seconds=50,
        last_watched_at=datetime(2026, 1, 4),
    )
    point_image = Image.create(origin="delete-all-point.webp")
    point = MediaPoint.create(
        media=source,
        image=point_image,
        movie_number=movie.movie_number,
        offset_seconds=10,
    )

    MediaService.delete_media(
        source.id,
        migration_excluded_media_ids=frozenset({source.id, sibling.id}),
    )

    assert Media.get_or_none(Media.id == source.id) is None
    assert Media.get_or_none(Media.id == sibling.id) is not None
    assert MediaProgress.get_or_none(
        MediaProgress.media == sibling.id
    ) is None
    assert MediaPoint.get_by_id(point.id).media_id is None
