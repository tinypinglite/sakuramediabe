"""互动数快照落库；热度由插件快照直接提供。"""

from __future__ import annotations

from src.common.runtime_time import utc_now_for_db
from src.model import Movie, get_database
from src.plugins.extensions.stats import PluginMovieStatsSnapshot


class MovieStatsService:
    @classmethod
    def apply_snapshot(
        cls,
        movie_id: int,
        *,
        plugin_id: str,
        snapshot: PluginMovieStatsSnapshot,
        heat_version: str,
    ) -> bool:
        values = dict(snapshot.values)
        with get_database().atomic():
            movie = Movie.get_or_none(Movie.id == movie_id)
            if movie is None:
                return False
            changed = (
                movie.interaction_stats != values
                or movie.heat != snapshot.heat
                or movie.stats_source != plugin_id
                or movie.heat_version != heat_version
            )
            Movie.update(
                interaction_stats=values,
                heat=snapshot.heat,
                stats_source=plugin_id,
                stats_updated_at=utc_now_for_db(),
                heat_version=heat_version,
            ).where(Movie.id == movie_id).execute()
        return changed
