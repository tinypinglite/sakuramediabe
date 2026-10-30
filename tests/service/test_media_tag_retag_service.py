from types import SimpleNamespace

from src.model import Media, MediaLibrary, MediaMediaTag, MediaTag, Movie
from src.service.playback.media_tag_retag_service import MediaTagRetagService


def test_media_tag_retag_is_idempotent_uses_snapshot_and_clears_disabled_tags(test_db):
    library = MediaLibrary.create(
        name="retag-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="RETAG-001", title="retag")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="RETAG-001.mp4",
        release_title="4K release",
        release_description="中文字幕",
    )
    filename_tag = MediaTag.create(
        name="by-file",
        include_terms=["RETAG-001"],
        exclude_terms=[],
        enabled=True,
    )
    snapshot_tag = MediaTag.create(
        name="by-snapshot",
        include_terms=["中文字幕"],
        exclude_terms=[],
        enabled=True,
    )
    disabled_tag = MediaTag.create(
        name="disabled-old",
        include_terms=["RETAG-001"],
        exclude_terms=[],
        enabled=False,
    )
    MediaMediaTag.create(media=media, tag=disabled_tag)
    events = []
    reporter = SimpleNamespace(emit=lambda **payload: events.append(payload))

    first = MediaTagRetagService.retag_all_jav_media(reporter=reporter)

    assert first == {
        "total_media": 1,
        "tagged_media": 1,
        "updated_media": 1,
        "cleared_media": 0,
        "manual_skipped": 0,
        "failed_media": 0,
    }
    assert set(
        MediaMediaTag.select(MediaMediaTag.tag).where(
            MediaMediaTag.media == media.id
        ).tuples()
    ) == {(filename_tag.id,), (snapshot_tag.id,)}
    assert events[-1]["current"] == 1
    assert events[-1]["total"] == 1

    second = MediaTagRetagService.retag_all_jav_media(reporter=reporter)
    assert second["updated_media"] == 0
    assert second["tagged_media"] == 1

    MediaTag.update(enabled=False).execute()
    cleared = MediaTagRetagService.retag_all_jav_media(reporter=reporter)
    assert cleared == {
        "total_media": 1,
        "tagged_media": 0,
        "updated_media": 1,
        "cleared_media": 1,
        "manual_skipped": 0,
        "failed_media": 0,
    }
    assert not MediaMediaTag.select().exists()


def test_media_tag_retag_only_processes_jav_media(test_db):
    from src.model import VideoItem

    library = MediaLibrary.create(
        name="retag-video-library",
        provider_key="test",
        provider_config={},
    )
    video = VideoItem.create(title="video")
    Media.create(
        video_item=video,
        library=library,
        file_name="video.mp4",
    )
    events = []

    stats = MediaTagRetagService.retag_all_jav_media(
        reporter=SimpleNamespace(emit=lambda **payload: events.append(payload))
    )

    assert stats["total_media"] == 0
    assert events == [
        {
            "current": 0,
            "total": 0,
            "text": "媒体文件标签重算 · 已完成 0/0 · 已标记 0 · 已更新 0 · 已清除 0 · 手动跳过 0 · 失败 0",
            "summary_patch": {
                "total_media": 0,
                "tagged_media": 0,
                "updated_media": 0,
                "cleared_media": 0,
                "manual_skipped": 0,
                "failed_media": 0,
            },
        }
    ]


def test_media_tag_retag_skips_manually_managed_media(test_db):
    library = MediaLibrary.create(
        name="retag-manual-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="RETAG-MANUAL-001", title="manual")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="RETAG-MANUAL-001.mp4",
        is_media_tags_manually_managed=True,
    )
    manual_tag = MediaTag.create(
        name="manual",
        include_terms=["not-auto"],
        exclude_terms=[],
        enabled=True,
    )
    MediaTag.create(
        name="auto",
        include_terms=["RETAG-MANUAL-001"],
        exclude_terms=[],
        enabled=True,
    )
    MediaMediaTag.create(media=media, tag=manual_tag)

    stats = MediaTagRetagService.retag_all_jav_media(
        reporter=SimpleNamespace(emit=lambda **payload: None)
    )

    assert stats == {
        "total_media": 1,
        "tagged_media": 0,
        "updated_media": 0,
        "cleared_media": 0,
        "manual_skipped": 1,
        "failed_media": 0,
    }
    assert set(
        MediaMediaTag.select(MediaMediaTag.tag)
        .where(MediaMediaTag.media == media.id)
        .tuples()
    ) == {(manual_tag.id,)}
