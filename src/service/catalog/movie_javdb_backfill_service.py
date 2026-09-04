"""插件影片按固定间隔尝试接入 JavDB，不依赖来源插件继续安装。"""

import time

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError
from src.model import Movie
from src.service.catalog.catalog_import_service import CatalogImportService


class MovieJavdbBackfillService:
    BATCH_SIZE = 50
    REQUEST_INTERVAL = 2

    def __init__(self, provider=None, import_service=None):
        self.provider = provider or build_javdb_provider()
        self.import_service = import_service or CatalogImportService()

    @staticmethod
    def pending():
        return Movie.select().where(
            Movie.javdb_id.is_null(True) & Movie.metadata_source.is_null(False)
        )

    def run(self, *, reporter):
        ids = [
            movie.id
            for movie in self.pending()
            .where(Movie.javdb_next_check_at <= utc_now_for_db())
            .order_by(Movie.javdb_next_check_at, Movie.id)
            .limit(self.BATCH_SIZE)
        ]
        stats = {
            "candidate_movies": len(ids),
            "succeeded_movies": 0,
            "not_found_movies": 0,
            "failed_movies": 0,
        }
        for current, movie_id in enumerate(ids, 1):
            if current > 1:
                time.sleep(self.REQUEST_INTERVAL)
            movie = self.pending().where(Movie.id == movie_id).get_or_none()
            if movie is None:
                reporter.emit(current=current, total=len(ids))
                continue
            try:
                detail = self.provider.get_movie_by_number(movie.movie_number)
                self.import_service.backfill_plugin_movie(movie, detail)
                stats["succeeded_movies"] += 1
            except MetadataNotFoundError:
                stats["not_found_movies"] += 1
                logger.info("JavDB 尚未收录 movie={}", movie.movie_number)
            except Exception as exc:
                stats["failed_movies"] += 1
                logger.warning(
                    "JavDB 补录失败 movie={} detail={}", movie.movie_number, exc
                )
            finally:
                Movie.update(
                    javdb_next_check_at=utc_now_for_db()
                    + self.import_service.JAVDB_CHECK_INTERVAL
                ).where((Movie.id == movie_id) & Movie.javdb_id.is_null(True)).execute()
                reporter.emit(current=current, total=len(ids))
        return stats
