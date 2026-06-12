from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    MovieSeries,
    VideoItem,
)
from src.schema.playback.clips import MediaClipCreateRequest, MediaClipUpdateRequest
from src.service.playback.media_clip_service import MediaClipService

CLIP_MODELS = [
    MovieSeries,
    Movie,
    Image,
    MediaLibrary,
    VideoItem,
    Media,
    MediaThumbnail,
    MediaClip,
    ClipCollection,
    ClipCollectionItem,
]


def _create_movie(number: str = "ABC-001") -> Movie:
    return Movie.create(movie_number=number, javdb_id=f"jav-{number}", title=number)


def _create_thumbnail(media: Media, offset: int) -> MediaThumbnail:
    path = f"movies/{media.movie.movie_number}/media/fp/thumbnails/{offset}.webp"
    image = Image.create(origin=path, small=path, medium=path, large=path)
    return MediaThumbnail.create(media=media, image=image, offset=offset)


@pytest.fixture()
def clip_env(test_db, tmp_path, monkeypatch):
    test_db.bind(CLIP_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(CLIP_MODELS)
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    monkeypatch.setattr(settings.media, "media_clip_max_duration_seconds", 900)

    # 用一个写入 target 的假切片替换真实 ffmpeg，验证编排而非外部进程。
    def _fake_cut(source_path: Path, target_path: Path, start: int, end: int) -> None:
        target_path.write_bytes(b"clip-bytes")

    monkeypatch.setattr(MediaClipService, "_cut_clip_file", staticmethod(_fake_cut))

    movie = _create_movie()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(source), valid=True)
    for offset in (0, 10, 20, 30, 40):
        _create_thumbnail(media, offset)
    yield media
    test_db.drop_tables(list(reversed(CLIP_MODELS)))


def _thumb_id(media: Media, offset: int) -> int:
    return MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == offset).id


def test_create_clip_cuts_file_and_persists(clip_env, tmp_path):
    media = clip_env
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 10),
        end_thumbnail_id=_thumb_id(media, 30),
        title=" 精彩片段 ",
    )

    resource, created = MediaClipService.create_clip(media.id, payload)

    assert created is True
    assert resource.start_offset_seconds == 10
    assert resource.end_offset_seconds == 30
    assert resource.title == "精彩片段"
    assert resource.movie_number == media.movie.movie_number
    assert resource.file_size_bytes > 0
    assert resource.cover_image is not None  # 区间首帧封面
    assert resource.stream_url.startswith("/media-clips/")
    clip = MediaClip.get_by_id(resource.clip_id)
    clip_path = Path(settings.media.media_clip_root_path) / clip.file_path
    assert clip_path.exists()


def test_create_clip_orders_offsets_regardless_of_input(clip_env):
    media = clip_env
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 40),
        end_thumbnail_id=_thumb_id(media, 20),
    )

    resource, _ = MediaClipService.create_clip(media.id, payload)

    assert resource.start_offset_seconds == 20
    assert resource.end_offset_seconds == 40


def test_create_clip_is_idempotent_on_same_range(clip_env):
    media = clip_env
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 0),
        end_thumbnail_id=_thumb_id(media, 20),
    )

    first, created_first = MediaClipService.create_clip(media.id, payload)
    second, created_second = MediaClipService.create_clip(media.id, payload)

    assert created_first is True
    assert created_second is False
    assert first.clip_id == second.clip_id
    assert MediaClip.select().count() == 1


def test_create_clip_rejects_same_thumbnail(clip_env):
    media = clip_env
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 10),
        end_thumbnail_id=_thumb_id(media, 10),
    )

    with pytest.raises(ApiError) as exc:
        MediaClipService.create_clip(media.id, payload)
    assert exc.value.code == "media_clip_invalid_range"


def test_create_clip_rejects_too_long_range(clip_env, monkeypatch):
    media = clip_env
    monkeypatch.setattr(settings.media, "media_clip_max_duration_seconds", 15)
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 0),
        end_thumbnail_id=_thumb_id(media, 40),
    )

    with pytest.raises(ApiError) as exc:
        MediaClipService.create_clip(media.id, payload)
    assert exc.value.code == "media_clip_too_long"


def test_create_clip_rejects_thumbnail_from_other_media(clip_env):
    media = clip_env
    other_media = Media.create(movie=media.movie, path=str(media.path) + ".2", valid=True)
    foreign_thumbnail = _create_thumbnail(other_media, 55)
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 0),
        end_thumbnail_id=foreign_thumbnail.id,
    )

    with pytest.raises(ApiError) as exc:
        MediaClipService.create_clip(media.id, payload)
    assert exc.value.code == "media_thumbnail_not_found"


def test_create_clip_cleans_up_on_cut_failure(clip_env, monkeypatch):
    media = clip_env

    def _boom(source_path, target_path, start, end):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(MediaClipService, "_cut_clip_file", staticmethod(_boom))
    payload = MediaClipCreateRequest(
        start_thumbnail_id=_thumb_id(media, 0),
        end_thumbnail_id=_thumb_id(media, 20),
    )

    with pytest.raises(ApiError) as exc:
        MediaClipService.create_clip(media.id, payload)
    assert exc.value.code == "media_clip_generation_failed"
    # 失败后不应残留占位记录。
    assert MediaClip.select().count() == 0


def test_get_clip_detail_returns_preview_frames(clip_env):
    media = clip_env
    resource, _ = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=_thumb_id(media, 10),
            end_thumbnail_id=_thumb_id(media, 30),
        ),
    )

    detail = MediaClipService.get_clip_detail(resource.clip_id)

    # 区间 [10, 30] 内的缩略图为 10/20/30 共三帧。
    assert len(detail.preview_frames) == 3


def test_delete_clip_removes_record_and_file(clip_env):
    media = clip_env
    resource, _ = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=_thumb_id(media, 0),
            end_thumbnail_id=_thumb_id(media, 10),
        ),
    )
    clip = MediaClip.get_by_id(resource.clip_id)
    clip_path = Path(settings.media.media_clip_root_path) / clip.file_path
    assert clip_path.exists()

    MediaClipService.delete_clip(resource.clip_id)

    assert MediaClip.get_or_none(MediaClip.id == resource.clip_id) is None
    assert not clip_path.exists()


def test_clip_survives_source_media_deletion(clip_env):
    media = clip_env
    resource, _ = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=_thumb_id(media, 0),
            end_thumbnail_id=_thumb_id(media, 10),
        ),
    )
    clip = MediaClip.get_by_id(resource.clip_id)
    clip_path = Path(settings.media.media_clip_root_path) / clip.file_path

    Media.delete().where(Media.id == media.id).execute()

    surviving = MediaClip.get_by_id(resource.clip_id)
    # 来源 Media 删除后，片段记录、文件与番号快照都保留，仅 media 引用置空。
    assert surviving.media_id is None
    assert surviving.movie_number == media.movie.movie_number
    assert clip_path.exists()


def test_update_clip_changes_title(clip_env):
    media = clip_env
    resource, _ = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=_thumb_id(media, 0),
            end_thumbnail_id=_thumb_id(media, 10),
        ),
    )

    updated = MediaClipService.update_clip(resource.clip_id, MediaClipUpdateRequest(title=" 新标题 "))

    assert updated.title == "新标题"
    assert MediaClip.get_by_id(resource.clip_id).title == "新标题"
