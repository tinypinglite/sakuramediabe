from datetime import date, datetime

from src.schema.catalog.movies import MovieListItemResource


class DailyRecommendationMovieResource(MovieListItemResource):
    snapshot_date: date
    generated_at: datetime
    rank: int
    recommendation_score: float = 0.0
    reason_codes: list[str]
    reason_texts: list[str]
    signal_scores: dict[str, float]
    is_stale: bool = False
