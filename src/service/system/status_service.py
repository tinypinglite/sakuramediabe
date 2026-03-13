from peewee import fn

from src.model import Actor, Media, MediaLibrary, Movie
from src.schema.system.status import (
    StatusActorSummary,
    StatusMediaFileSummary,
    StatusMediaLibrarySummary,
    StatusMovieSummary,
    StatusResource,
)


class StatusService:
    FEMALE_GENDER = 1

    @classmethod
    def get_status(cls) -> StatusResource:
        female_total = Actor.select().where(Actor.gender == cls.FEMALE_GENDER).count()
        female_subscribed = (
            Actor.select()
            .where((Actor.gender == cls.FEMALE_GENDER) & (Actor.is_subscribed == True))
            .count()
        )

        movie_total = Movie.select().count()
        movie_subscribed = Movie.select().where(Movie.is_subscribed == True).count()
        movie_playable = (
            Media.select(fn.COUNT(fn.DISTINCT(Media.movie)))
            .where(Media.valid == True)
            .scalar()
            or 0
        )

        media_file_total = Media.select().count()
        media_file_total_size_bytes = (
            Media.select(fn.COALESCE(fn.SUM(Media.file_size_bytes), 0)).scalar() or 0
        )

        media_library_total = MediaLibrary.select().count()

        return StatusResource(
            actors=StatusActorSummary(
                female_total=int(female_total),
                female_subscribed=int(female_subscribed),
            ),
            movies=StatusMovieSummary(
                total=int(movie_total),
                subscribed=int(movie_subscribed),
                playable=int(movie_playable),
            ),
            media_files=StatusMediaFileSummary(
                total=int(media_file_total),
                total_size_bytes=int(media_file_total_size_bytes),
            ),
            media_libraries=StatusMediaLibrarySummary(total=int(media_library_total)),
        )
