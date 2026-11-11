"""插件互动数写入与热度重算。"""

from __future__ import annotations

from typing import Any

from src.common.runtime_time import utc_now_for_db
from src.model import Movie, get_database
from src.plugins.extensions.stats import PluginMovieStats
from src.service.catalog.movie_heat_service import MovieHeatService


class MovieStatsService:
    @classmethod
    def update(cls, movie_id: int, stats: PluginMovieStats | dict[str, Any]) -> bool:
        payload = (
            stats
            if isinstance(stats, PluginMovieStats)
            else PluginMovieStats.model_validate(stats)
        )
        fields = payload.model_dump(exclude_none=True)
        if not fields:
            return False
        with get_database().atomic():
            movie = Movie.get_or_none(Movie.id == movie_id)
            if movie is None:
                return False
            changed = any(getattr(movie, name) != value for name, value in fields.items())
            if not changed:
                return False
            fields["updated_at"] = utc_now_for_db()
            Movie.update(**fields).where(Movie.id == movie_id).execute()
            MovieHeatService.update_single_movie_heat(movie_id)
        return True
