from loguru import logger

from src.model import Movie
from src.service.catalog.catalog_import_service import CatalogImportService


class _NoopDmmProvider:
    def get_movie_desc(self, movie_number: str) -> str:
        raise RuntimeError(f"movie_desc_not_supported:{movie_number}")


class MovieThinCoverBackfillService:
    """批量为已有影片补算竖封面图。"""

    def __init__(self, import_service: CatalogImportService | None = None):
        self.import_service = import_service or CatalogImportService(dmm_provider=_NoopDmmProvider())

    def backfill_missing_thin_cover_images(self) -> dict[str, int]:
        stats = {
            "scanned_movies": 0,
            "updated_movies": 0,
            "skipped_movies": 0,
            "failed_movies": 0,
        }
        query = Movie.select().where(Movie.thin_cover_image.is_null(True)).order_by(Movie.id)
        for movie in query:
            stats["scanned_movies"] += 1
            try:
                updated = self.import_service.backfill_movie_thin_cover(movie)
            except Exception as exc:
                stats["failed_movies"] += 1
                logger.exception(
                    "Movie thin cover backfill failed movie_id={} movie_number={} detail={}",
                    movie.id,
                    movie.movie_number,
                    exc,
                )
                continue

            if updated:
                stats["updated_movies"] += 1
            else:
                stats["skipped_movies"] += 1
        return stats
