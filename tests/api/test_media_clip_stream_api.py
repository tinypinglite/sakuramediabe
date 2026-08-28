from src.common import build_signed_clip_url
from src.model import MediaClip


def test_media_clip_stream_cleans_empty_placeholder_and_returns_404(
    client,
    test_db,
):
    clip = MediaClip.create(
        movie_number="STREAM-001",
        start_offset_seconds=0,
        end_offset_seconds=10,
        file_path="",
        file_size_bytes=0,
        duration_seconds=0,
    )

    response = client.get(build_signed_clip_url(clip.id))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_clip_not_found"
    assert MediaClip.get_or_none(MediaClip.id == clip.id) is None
