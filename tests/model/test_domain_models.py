from src.model import (
    Actor,
    DownloadClient,
    DownloadTask,
    Image,
    ImageSearchSession,
    ImportJob,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
)


def test_all_documented_domain_models_can_create_tables(test_db):
    models = [
        Image,
        Tag,
        Actor,
        Movie,
        MovieActor,
        MoviePlotImage,
        MovieTag,
        Playlist,
        PlaylistMovie,
        MediaLibrary,
        Media,
        MediaThumbnail,
        MediaProgress,
        MediaPoint,
        ImageSearchSession,
        DownloadClient,
        DownloadTask,
        ImportJob,
    ]

    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    for model in models:
        assert model.table_exists()


def test_media_model_does_not_have_media_type_field():
    assert "media_type" not in Media._meta.fields


def test_media_model_uses_valid_field_for_file_availability():
    assert "valid" in Media._meta.fields
    assert "is_playable" not in Media._meta.fields


def test_media_model_tracks_library_and_storage_mode():
    assert "library" in Media._meta.fields
    assert "storage_mode" in Media._meta.fields
    assert "special_tags" in Media._meta.fields
    assert "need_mtn" in Media._meta.fields
    assert "mtn_retry_count" in Media._meta.fields
    assert "mtn_last_error" in Media._meta.fields
    assert "relative_path" not in Media._meta.fields


def test_media_model_defaults_thumbnail_generation_state(test_db):
    models = [Image, Movie, MediaLibrary, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    movie = Movie.create(javdb_id="javdb-001", movie_number="ABC-001", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4")

    assert media.need_mtn is True
    assert media.mtn_retry_count == 0
    assert media.mtn_last_error is None


def test_media_thumbnail_model_tracks_offset_and_joytag_status(test_db):
    models = [Image, Movie, MediaLibrary, Media, MediaThumbnail]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    assert "offset" in MediaThumbnail._meta.fields
    assert "joytag_index_status" in MediaThumbnail._meta.fields
    assert "offset_seconds" not in MediaThumbnail._meta.fields

    movie = Movie.create(javdb_id="javdb-001", movie_number="ABC-001", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4")
    image = Image.create(origin="thumb.jpg", small="thumb.jpg", medium="thumb.jpg", large="thumb.jpg")
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=60)

    assert thumbnail.offset == 60
    assert thumbnail.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING


def test_single_user_business_models_drop_user_ownership():
    assert "owner" not in Playlist._meta.fields
    assert "user" not in MediaProgress._meta.fields
    assert "user" not in MediaPoint._meta.fields
    assert "user" not in ImageSearchSession._meta.fields
    assert "user" not in DownloadClient._meta.fields
    assert "user" not in DownloadTask._meta.fields
    assert "user" not in ImportJob._meta.fields


def test_catalog_models_inline_subscription_flags():
    assert "is_subscribed" in Movie._meta.fields
    assert "is_subscribed" in Actor._meta.fields
    assert "javdb_id" in Movie._meta.fields
    assert "javdb_id" in Actor._meta.fields
    assert "extra" in Movie._meta.fields
    assert "movie_number" in Movie._meta.fields
    assert "release_date" in Movie._meta.fields
    assert "duration_minutes" in Movie._meta.fields
    assert "is_collection" in Movie._meta.fields


def test_image_search_session_model_tracks_runtime_query_state(test_db):
    models = [ImageSearchSession]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    assert "query_vector" in ImageSearchSession._meta.fields
    assert "movie_ids" in ImageSearchSession._meta.fields
    assert "exclude_movie_ids" in ImageSearchSession._meta.fields
    assert "next_cursor" in ImageSearchSession._meta.fields
    assert "expires_at" in ImageSearchSession._meta.fields

    session = ImageSearchSession.create(
        session_id="session-1",
        page_size=20,
        query_vector=[0.1, 0.2, 0.3],
        movie_ids=[1, 2],
        exclude_movie_ids=[3],
        next_cursor="cursor-1",
        score_threshold=0.8,
        expires_at="2026-03-13 10:00:00",
    )

    assert session.query_vector == [0.1, 0.2, 0.3]
    assert session.movie_ids == [1, 2]
    assert session.exclude_movie_ids == [3]
    assert session.next_cursor == "cursor-1"
    assert session.score_threshold == 0.8


def test_movie_model_tracks_interaction_fields_and_plot_images():
    assert "watched_count" in Movie._meta.fields
    assert "want_watch_count" in Movie._meta.fields
    assert "comment_count" in Movie._meta.fields
    assert "score_number" in Movie._meta.fields
    assert "subscribed_at" in Movie._meta.fields
    assert MoviePlotImage._meta.table_name == "movie_plot_image"
