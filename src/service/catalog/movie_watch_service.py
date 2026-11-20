"""影片观看状态：进度摘要、列表筛选表达式与手动标记。

已看口径（与前端续播策略保持方向一致，但更宽松）：

- 有进度且 ``duration > 0``；
- 已看比例 >= 80%（``position * 5 >= duration * 4``）；
- 距结尾不超过 600 秒。

三者同时满足才算已看，避免短片刚开播就被固定 600 秒阈值误判。剩余的
10 秒/30 秒续播判断仍由播放器自己负责，不受这里影响。
"""

from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record
from src.model import Media, MediaProgress, Movie, get_database
from src.schema.catalog.movies import (
    MovieWatchProgressResource,
    MovieWatchStateResource,
    MovieWatchStatus,
)

WATCHED_REMAINING_SECONDS = 600
WATCHED_MIN_RATIO_NUMERATOR = 4
WATCHED_MIN_RATIO_DENOMINATOR = 5
WATCHING_MIN_POSITION_SECONDS = 10


def is_watch_finished(position_seconds: int, duration_seconds: int) -> bool:
    """按统一口径判断单条媒体进度是否算已看完。"""
    if duration_seconds <= 0:
        return False
    if position_seconds * WATCHED_MIN_RATIO_DENOMINATOR < (
        duration_seconds * WATCHED_MIN_RATIO_NUMERATOR
    ):
        return False
    return duration_seconds - position_seconds <= WATCHED_REMAINING_SECONDS


def _finished_media_exists():
    finished_media = (
        Media.select(Media.id)
        .join(MediaProgress, JOIN.INNER, on=(MediaProgress.media == Media.id))
        .where(
            Media.movie == Movie.movie_number,
            Media.duration_seconds > 0,
            MediaProgress.position_seconds * WATCHED_MIN_RATIO_DENOMINATOR
            >= Media.duration_seconds * WATCHED_MIN_RATIO_NUMERATOR,
            Media.duration_seconds - MediaProgress.position_seconds
            <= WATCHED_REMAINING_SECONDS,
        )
    )
    return fn.EXISTS(finished_media)


def _progress_media_exists():
    watched_media = (
        Media.select(Media.id)
        .join(MediaProgress, JOIN.INNER, on=(MediaProgress.media == Media.id))
        .where(
            Media.movie == Movie.movie_number,
            MediaProgress.position_seconds > WATCHING_MIN_POSITION_SECONDS,
        )
    )
    return fn.EXISTS(watched_media)


def watch_status_expression(status: MovieWatchStatus):
    """返回影片列表可用的 EXISTS 过滤表达式；``ALL`` 返回 None。"""
    if status == MovieWatchStatus.WATCHED:
        return _finished_media_exists()
    if status == MovieWatchStatus.WATCHING:
        return _progress_media_exists() & ~_finished_media_exists()
    if status == MovieWatchStatus.UNWATCHED:
        return ~_progress_media_exists()
    return None


def _latest_progress_rows(movie_numbers: set[str]) -> list[dict]:
    return list(
        Media.select(
            Media.movie.alias("movie_number"),
            Media.id.alias("media_id"),
            Media.duration_seconds,
            MediaProgress.position_seconds,
            MediaProgress.last_watched_at,
        )
        .join(MediaProgress, JOIN.INNER, on=(MediaProgress.media == Media.id))
        .where(Media.movie.in_(movie_numbers))
        .order_by(
            MediaProgress.last_watched_at.is_null(),
            MediaProgress.last_watched_at.desc(),
            Media.id.desc(),
        )
        .dicts()
    )


def attach_watch_progress(movies) -> None:
    """给当前页影片挂上最近一条媒体进度摘要（每页一次批量查询）。

    ``finished`` 采用影片级口径：任一媒体看完即视为影片已看，与列表
    ``watch_status`` 筛选一致；位置/时长仍取最近观看的媒体。
    """
    movie_numbers = {
        movie.movie_number
        for movie in movies
        if getattr(movie, "movie_number", None)
    }
    if not movie_numbers:
        return

    latest_rows: dict[str, dict] = {}
    finished_movie_numbers: set[str] = set()
    for row in _latest_progress_rows(movie_numbers):
        movie_number = row["movie_number"]
        latest_rows.setdefault(movie_number, row)
        duration_seconds = int(row["duration_seconds"] or 0)
        position_seconds = int(row["position_seconds"] or 0)
        if is_watch_finished(position_seconds, duration_seconds):
            finished_movie_numbers.add(movie_number)

    progress_by_movie = {
        movie_number: MovieWatchProgressResource(
            media_id=int(row["media_id"]),
            position_seconds=int(row["position_seconds"] or 0),
            duration_seconds=int(row["duration_seconds"] or 0),
            last_watched_at=row["last_watched_at"],
            finished=movie_number in finished_movie_numbers,
        )
        for movie_number, row in latest_rows.items()
    }

    for movie in movies:
        movie.watch_progress = progress_by_movie.get(movie.movie_number)


class MovieWatchService:
    @classmethod
    def set_watch_state(
        cls,
        movie_number: str,
        state: str,
    ) -> MovieWatchStateResource:
        movie = require_record(
            Movie,
            Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="影片不存在",
            error_details={"movie_number": movie_number},
        )
        media_items = list(Media.select().where(Media.movie == movie.movie_number))
        if not media_items:
            raise ApiError(
                409,
                "movie_has_no_media",
                "影片没有媒体，无法标记观看状态",
                {"movie_number": movie_number},
            )

        now = utc_now_for_db()
        with get_database().atomic():
            if state == "watched":
                updated = 0
                for media in media_items:
                    duration_seconds = int(media.duration_seconds or 0)
                    if duration_seconds <= 0:
                        continue
                    progress = MediaProgress.get_or_none(
                        MediaProgress.media == media
                    )
                    if progress is None:
                        MediaProgress.create(
                            media=media,
                            position_seconds=duration_seconds,
                            last_watched_at=now,
                        )
                    else:
                        progress.position_seconds = duration_seconds
                        progress.last_watched_at = now
                        progress.save()
                    updated += 1
                if updated == 0:
                    raise ApiError(
                        409,
                        "movie_duration_unavailable",
                        "媒体时长未知，无法标记已看",
                        {"movie_number": movie_number},
                    )
            else:
                MediaProgress.delete().where(
                    MediaProgress.media.in_([media.id for media in media_items])
                ).execute()

        return MovieWatchStateResource(movie_number=movie_number, state=state)
