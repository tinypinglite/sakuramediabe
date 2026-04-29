from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.model import Media, Movie
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
    query = (
        Media.select(Media.id, Media.path, Media.valid, Movie.movie_number, Movie.title)
        .join(Movie, on=(Media.movie == Movie.movie_number))
        .where(Media.id.in_(normalized_ids))
        .order_by(Media.id.asc())
    )
    return {
        media.id: TaskRecordResourceSummary(
            resource_id=media.id,
            movie_number=media.movie.movie_number,
            title=media.movie.title,
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
        .join(Movie, on=(Media.movie == Movie.movie_number))
        .where(
            (Movie.movie_number.contains(normalized_search))
            | (Movie.title.contains(normalized_search))
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
