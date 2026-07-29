from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from peewee import JOIN

from src.model import Media, Movie, VideoItem
from src.schema.system.resource_task_state import TaskRecordResourceSummary


def _normalize_search(search: str | None) -> str | None:
    normalized = str(search or "").strip()
    return normalized or None


@dataclass(frozen=True)
class ResourceTaskRecordResolver:
    resolve_summaries: Callable[[list[int]], dict[int, TaskRecordResourceSummary]]
    search_resource_ids: Callable[[str], list[int]]


def _resolve_movie_summaries(resource_ids: list[int]) -> dict[int, TaskRecordResourceSummary]:
    normalized_ids = [int(resource_id) for resource_id in resource_ids]
    if not normalized_ids:
        return {}
    query = (
        Movie.select(Movie.id, Movie.movie_number, Movie.title)
        .where(Movie.id.in_(normalized_ids))
        .order_by(Movie.id.asc())
    )
    return {
        movie.id: TaskRecordResourceSummary(
            resource_id=movie.id,
            movie_number=movie.movie_number,
            title=movie.title,
        )
        for movie in query
    }


def _search_movie_resource_ids(search: str) -> list[int]:
    normalized_search = _normalize_search(search)
    if normalized_search is None:
        return []
    query = (
        Movie.select(Movie.id)
        .where(
            (Movie.movie_number.contains(normalized_search))
            | (Movie.title.contains(normalized_search))
            | (Movie.javdb_id.contains(normalized_search))
        )
        .order_by(Movie.id.asc())
    )
    return [movie.id for movie in query]


def _resolve_media_summaries(resource_ids: list[int]) -> dict[int, TaskRecordResourceSummary]:
    normalized_ids = [int(resource_id) for resource_id in resource_ids]
    if not normalized_ids:
        return {}
    # 非 JAV 媒体没有 movie，Movie/VideoItem 均 LEFT OUTER，标题按归属取。
    query = (
        Media.select(
            Media.id,
            Media.path,
            Media.valid,
            Movie.movie_number,
            Movie.title,
            VideoItem.title,
        )
        .join(Movie, JOIN.LEFT_OUTER, on=(Media.movie == Movie.movie_number))
        .switch(Media)
        .join(VideoItem, JOIN.LEFT_OUTER)
        .where(Media.id.in_(normalized_ids))
        .order_by(Media.id.asc())
    )
    return {
        media.id: TaskRecordResourceSummary(
            resource_id=media.id,
            movie_number=media.movie.movie_number if media.movie_number else None,
            title=media.movie.title if media.movie_number else (
                media.video_item.title if media.video_item_id else None
            ),
            path=media.path,
            valid=media.valid,
        )
        for media in query
    }


def _search_media_resource_ids(search: str) -> list[int]:
    normalized_search = _normalize_search(search)
    if normalized_search is None:
        return []
    query = (
        Media.select(Media.id)
        .join(Movie, JOIN.LEFT_OUTER, on=(Media.movie == Movie.movie_number))
        .switch(Media)
        .join(VideoItem, JOIN.LEFT_OUTER)
        .where(
            (Movie.movie_number.contains(normalized_search))
            | (Movie.title.contains(normalized_search))
            | (VideoItem.title.contains(normalized_search))
            | (Media.path.contains(normalized_search))
        )
        .order_by(Media.id.asc())
    )
    return [media.id for media in query]


MOVIE_TASK_RECORD_RESOLVER = ResourceTaskRecordResolver(
    resolve_summaries=_resolve_movie_summaries,
    search_resource_ids=_search_movie_resource_ids,
)

MEDIA_TASK_RECORD_RESOLVER = ResourceTaskRecordResolver(
    resolve_summaries=_resolve_media_summaries,
    search_resource_ids=_search_media_resource_ids,
)


# ---- 统一 action 协议的领域合格性钩子 ----
# action service 在状态判定前先跑本钩子，把领域上不成立的资源逐条落 skipped
# （替代旧的任务专用端点里散落的 422 前置校验），返回 {resource_id: skip_reason}。

SKIP_REASON_MOVIE_NOT_FOUND = "movie_not_found"
SKIP_REASON_MOVIE_NOT_SUBSCRIBED = "movie_not_subscribed"
SKIP_REASON_MEDIA_NOT_FOUND = "media_not_found"
SKIP_REASON_MEDIA_INVALID = "media_invalid"


def build_movie_actionable_check(
    *,
    required_attr: str | None = None,
    missing_reason: str | None = None,
    require_subscribed: bool = False,
) -> Callable[[list[int]], dict[int, str]]:
    """影片类任务的合格性钩子：影片必须存在，可叠加"必填字段非空 / 已订阅"约束。"""

    def check(resource_ids: list[int]) -> dict[int, str]:
        normalized_ids = [int(resource_id) for resource_id in resource_ids]
        if not normalized_ids:
            return {}
        columns = [Movie.id, Movie.is_subscribed]
        if required_attr is not None:
            columns.append(getattr(Movie, required_attr))
        movies = {
            movie.id: movie
            for movie in Movie.select(*columns).where(Movie.id.in_(normalized_ids))
        }
        ineligible: dict[int, str] = {}
        for resource_id in normalized_ids:
            movie = movies.get(resource_id)
            if movie is None:
                ineligible[resource_id] = SKIP_REASON_MOVIE_NOT_FOUND
            elif require_subscribed and not movie.is_subscribed:
                ineligible[resource_id] = SKIP_REASON_MOVIE_NOT_SUBSCRIBED
            elif required_attr is not None and not str(
                getattr(movie, required_attr) or ""
            ).strip():
                ineligible[resource_id] = missing_reason
        return ineligible

    return check


def media_actionable_check(resource_ids: list[int]) -> dict[int, str]:
    """媒体类任务的合格性钩子：媒体必须存在且 valid（失效媒体重置只会留死 pending 行）。"""
    normalized_ids = [int(resource_id) for resource_id in resource_ids]
    if not normalized_ids:
        return {}
    media_rows = {
        media.id: media
        for media in Media.select(Media.id, Media.valid).where(Media.id.in_(normalized_ids))
    }
    ineligible: dict[int, str] = {}
    for resource_id in normalized_ids:
        media = media_rows.get(resource_id)
        if media is None:
            ineligible[resource_id] = SKIP_REASON_MEDIA_NOT_FOUND
        elif not media.valid:
            ineligible[resource_id] = SKIP_REASON_MEDIA_INVALID
    return ineligible
