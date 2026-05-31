from typing import ClassVar

from src.model import PLAYLIST_KIND_CUSTOM, SYSTEM_PLAYLIST_KINDS
from src.schema.common.base import SchemaModel


class PlaylistSummaryResource(SchemaModel):
    SYSTEM_KINDS: ClassVar[set[str]] = set(SYSTEM_PLAYLIST_KINDS)

    id: int
    name: str
    kind: str = PLAYLIST_KIND_CUSTOM
    is_system: bool

    @classmethod
    def from_playlist(cls, playlist) -> "PlaylistSummaryResource":
        return cls.from_peewee_model(
            playlist,
            extra={
                "is_system": playlist.kind in cls.SYSTEM_KINDS,
            },
        )
