from types import SimpleNamespace

import pytest

from src.model import (
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaTag,
    Movie,
    VideoItem,
)
from src.schema.playback.media_tags import MediaTagUpsertRequest
from src.service.playback.media_tag_retag_service import MediaTagRetagService
from src.service.playback.media_tag_service import (
    MediaTagRule,
    MediaTagService,
)


def test_media_tag_matching_uses_nfkc_casefold_and_substring():
    rules = (
        MediaTagRule(
            tag_id=1,
            name="subtitle",
            include_terms=("中文字幕",),
            exclude_terms=(),
        ),
    )

    matches = MediaTagService.match_rules(
        rules,
        file_name="ABC-001_中文字幕.mp4",
    )

    assert [(match.tag_id, match.matched_term) for match in matches] == [
        (1, "中文字幕")
    ]

    full_width = (
        MediaTagRule(
            tag_id=2,
            name="4K",
            include_terms=("４Ｋ",),
            exclude_terms=(),
        ),
    )
    assert [
        match.tag_id
        for match in MediaTagService.match_rules(
            full_width,
            release_title="MOVIE 4k HDR",
        )
    ] == [2]


def test_media_tag_matching_prefers_any_exclusion_and_allows_multiple_tags():
    rules = (
        MediaTagRule(
            tag_id=1,
            name="hd",
            include_terms=("1080p",),
            exclude_terms=("sample",),
        ),
        MediaTagRule(
            tag_id=2,
            name="archive",
            include_terms=("classic",),
            exclude_terms=(),
        ),
        MediaTagRule(
            tag_id=3,
            name="watermark",
            include_terms=("1080p",),
            exclude_terms=("watermark",),
        ),
    )

    matches = MediaTagService.match_rules(
        rules,
        file_name="MOVIE-1080p.mkv",
        release_title="Classic release",
        release_description="No watermark",
    )

    assert [match.tag_id for match in matches] == [1, 2]
    assert [
        match.tag_id
        for match in MediaTagService.match_rules(
            rules,
            file_name="MOVIE-1080p-SAMPLE.mkv",
        )
    ] == [3]


def test_media_tag_terms_are_trimmed_and_casefold_deduplicated(test_db):
    tag = MediaTagService.create_tag(
        MediaTagUpsertRequest(
            name="dedupe",
            include_terms=["4K", " 4k ", "４Ｋ"],
            exclude_terms=[" Sample ", "sample"],
            enabled=True,
        )
    )

    assert tag.include_terms == ["4K"]
    assert tag.exclude_terms == ["Sample"]


def test_load_active_rules_excludes_disabled_tags(test_db):
    MediaTag.create(
        name="enabled",
        include_terms=["keep"],
        exclude_terms=[],
        enabled=True,
    )
    MediaTag.create(
        name="disabled",
        include_terms=["keep"],
        exclude_terms=[],
        enabled=False,
    )

    rules = MediaTagService.load_active_rules()

    assert [rule.name for rule in rules] == ["enabled"]


def test_manual_assignment_accepts_disabled_tags_and_skips_auto_rules(test_db):
    library = MediaLibrary.create(
        name="manual-assignment",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="MANUAL-001", title="manual")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="MANUAL-001.4k.mp4",
    )
    manual_tag = MediaTag.create(
        name="manual-disabled",
        include_terms=["never-auto"],
        exclude_terms=[],
        enabled=False,
    )
    auto_tag = MediaTag.create(
        name="auto-match",
        include_terms=["MANUAL-001"],
        exclude_terms=[],
        enabled=True,
    )

    assignment = MediaTagService.set_manual_assignment(media.id, [manual_tag.id])

    assert assignment.is_media_tags_manually_managed is True
    assert [tag.id for tag in assignment.tags] == [manual_tag.id]
    assert set(
        MediaMediaTag.select(MediaMediaTag.tag)
        .where(MediaMediaTag.media == media.id)
        .tuples()
    ) == {(manual_tag.id,)}

    reset = MediaTagService.reset_media_assignment_to_auto(media.id)

    assert reset.is_media_tags_manually_managed is False
    assert [tag.id for tag in reset.tags] == [auto_tag.id]


def test_manual_assignment_survives_auto_retag(test_db):
    library = MediaLibrary.create(
        name="manual-assignment-retag",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="MANUAL-002", title="manual")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="MANUAL-002.4k.mp4",
    )
    manual_tag = MediaTag.create(
        name="manual-only",
        include_terms=["never-auto"],
        exclude_terms=[],
        enabled=True,
    )
    MediaTag.create(
        name="auto-match",
        include_terms=["MANUAL-002"],
        exclude_terms=[],
        enabled=True,
    )

    MediaTagService.set_manual_assignment(media.id, [manual_tag.id])

    stats = MediaTagRetagService.retag_all_jav_media(
        reporter=SimpleNamespace(emit=lambda **payload: None)
    )

    assert stats["manual_skipped"] == 1
    assert stats["failed_media"] == 0
    assert set(
        MediaMediaTag.select(MediaMediaTag.tag)
        .where(MediaMediaTag.media == media.id)
        .tuples()
    ) == {(manual_tag.id,)}


def test_media_tag_media_count_only_counts_valid_jav_media(test_db):
    library = MediaLibrary.create(
        name="media-tag-count",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="COUNT-001", title="count")
    video = VideoItem.create(title="video")
    tag = MediaTag.create(
        name="counted",
        include_terms=["counted"],
        exclude_terms=[],
        enabled=True,
    )
    MediaTag.create(
        name="empty",
        include_terms=["empty"],
        exclude_terms=[],
        enabled=True,
    )
    invalid_only_tag = MediaTag.create(
        name="invalid-only",
        include_terms=["invalid-only"],
        exclude_terms=[],
        enabled=True,
    )
    valid_media = Media.create(
        movie=movie,
        library=library,
        file_name="valid.mp4",
    )
    invalid_media = Media.create(
        movie=movie,
        library=library,
        file_name="invalid.mp4",
        valid=False,
    )
    video_media = Media.create(
        video_item=video,
        library=library,
        file_name="video.mp4",
    )
    for media in (valid_media, invalid_media, video_media):
        MediaMediaTag.create(media=media, tag=tag)
    MediaMediaTag.create(media=invalid_media, tag=invalid_only_tag)

    listed = MediaTagService.list_tags()
    updated = MediaTagService.update_tag(
        tag.id,
        MediaTagUpsertRequest(
            name="counted",
            include_terms=["counted"],
            exclude_terms=[],
            enabled=True,
        ),
    )

    media_counts = {item.name: item.media_count for item in listed}
    assert media_counts == {"counted": 1, "empty": 0, "invalid-only": 0}
    assert updated.media_count == 1


@pytest.mark.parametrize(
    "terms",
    [
        [],
        [" "],
        ["x" * 101],
    ],
)
def test_media_tag_create_rejects_invalid_include_terms(terms):
    with pytest.raises(ValueError):
        MediaTagUpsertRequest(
            name="invalid",
            include_terms=terms,
            exclude_terms=[],
            enabled=True,
        )
