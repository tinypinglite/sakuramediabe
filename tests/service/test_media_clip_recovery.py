from pathlib import Path
from types import SimpleNamespace

from src.config.config import settings
from src.model import Image, Media, MediaClip, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY, ClipArtifact
from src.schema.playback.clips import MediaClipCreateRequest
from src.service.playback import media_clip_service
from src.service.playback.media_clip_service import MediaClipService


def test_invalid_clip_placeholder_is_removed_and_regenerated(
    test_db,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    library = MediaLibrary.create(name="clip-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="CLIP-001", javdb_id="clip-1", title="clip")
    media = Media.create(movie=movie, library=library, file_name="clip.mp4")
    first_image = Image.create(origin="clip-first.webp", small="clip-first.webp", medium="clip-first.webp", large="clip-first.webp")
    second_image = Image.create(origin="clip-second.webp", small="clip-second.webp", medium="clip-second.webp", large="clip-second.webp")
    start_thumbnail = MediaThumbnail.create(media=media, image=first_image, offset=0)
    end_thumbnail = MediaThumbnail.create(media=media, image=second_image, offset=10)
    stale = MediaClip.create(
        media=media,
        movie_number=movie.movie_number,
        start_offset_seconds=0,
        end_offset_seconds=10,
        file_path="",
        file_size_bytes=0,
        duration_seconds=0,
    )

    class Storage:
        def create_clip(self, *, workspace, **_kwargs):
            (workspace / "clip.mp4").write_bytes(b"valid clip")
            return ClipArtifact(relative_path="clip.mp4")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    monkeypatch.setattr(
        media_clip_service.MediaMetadataProbeService,
        "probe_file",
        lambda _path: SimpleNamespace(duration_seconds=10),
    )

    resource, created = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=start_thumbnail.id,
            end_thumbnail_id=end_thumbnail.id,
            title="clip",
        ),
    )

    assert created is True
    assert resource.clip_id != stale.id
    assert MediaClip.get_or_none(MediaClip.id == stale.id) is None
    assert MediaClipService.list_media_clips().total == 1
