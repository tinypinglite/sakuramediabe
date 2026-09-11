from collections.abc import Sequence

from src.model.catalog.movies import Movie


def attach_movie_list_media(movies: Sequence[Movie]) -> None:
    from src.service.playback.media_summary_service import list_movie_media_summaries

    summaries = list_movie_media_summaries([movie.movie_number for movie in movies])
    for movie in movies:
        # 显式覆盖同名反向关联，保证 schema 序列化不会逐影片查询 Media。
        movie.media_items = summaries.get(movie.movie_number, [])
        movie.media_count = len(movie.media_items)
        movie.can_play = any(media.valid for media in movie.media_items)
