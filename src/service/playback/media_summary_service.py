from collections.abc import Sequence

from peewee import JOIN

from src.model.playback.libraries import MediaLibrary
from src.model.playback.media import Media
from src.schema.common.media import MediaSummaryResource


def list_movie_media_summaries(
    movie_numbers: Sequence[str],
) -> dict[str, list[MediaSummaryResource]]:
    """只查询当前页影片的展示字段；媒体库一起取出，避免外键懒加载。"""
    if not movie_numbers:
        return {}
    rows = (
        Media.select(
            Media.movie.alias("movie_number"),
            Media.id,
            Media.library.alias("library_id"),
            MediaLibrary.name.alias("library_name"),
            MediaLibrary.provider_key,
            Media.file_name,
            Media.resolution,
            Media.file_size_bytes,
            Media.duration_seconds,
            Media.video_info,
            Media.valid,
        )
        .join(MediaLibrary, JOIN.LEFT_OUTER)
        .where(Media.movie.in_(set(movie_numbers)))
        .order_by(Media.movie, Media.id)
        .dicts()
    )
    summaries: dict[str, list[MediaSummaryResource]] = {}
    for row in rows:
        summaries.setdefault(row["movie_number"], []).append(
            MediaSummaryResource.model_validate(row)
        )
    return summaries
