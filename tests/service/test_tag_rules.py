from src.service.transfers.tag_rules import build_media_special_tags, detect_candidate_tags


def _build_video_info(width: int, height: int) -> dict:
    return {
        "container": {"format_name": "mp4"},
        "video": {"codec_name": "h264", "profile": "Main", "width": width, "height": height},
        "audio": None,
        "subtitles": [],
    }


def test_build_media_special_tags_marks_4k_for_3840x2160():
    result = build_media_special_tags(
        ["/library/ABC-401.mp4"],
        "ABC-401",
        video_info=_build_video_info(3840, 2160),
    )

    assert result == "4K"


def test_build_media_special_tags_marks_4k_for_4096x2160():
    result = build_media_special_tags(
        ["/library/ABC-402.mp4"],
        "ABC-402",
        video_info=_build_video_info(4096, 2160),
    )

    assert result == "4K"


def test_build_media_special_tags_does_not_mark_4k_from_file_name_only():
    result = build_media_special_tags(
        ["/library/ABC-403-4K.mp4"],
        "ABC-403",
        video_info=_build_video_info(1920, 1080),
    )

    assert result == "普通"


def test_detect_candidate_tags_keeps_existing_remote_4k_heuristics():
    result = detect_candidate_tags("ABC-404 4K 中文字幕", "ABC-404", 10)

    assert result == ["中字", "4K"]
