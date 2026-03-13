from src.schema.common.base import SchemaModel


class StatusActorSummary(SchemaModel):
    female_total: int
    female_subscribed: int


class StatusMovieSummary(SchemaModel):
    total: int
    subscribed: int
    playable: int


class StatusMediaFileSummary(SchemaModel):
    total: int
    total_size_bytes: int


class StatusMediaLibrarySummary(SchemaModel):
    total: int


class StatusResource(SchemaModel):
    actors: StatusActorSummary
    movies: StatusMovieSummary
    media_files: StatusMediaFileSummary
    media_libraries: StatusMediaLibrarySummary
