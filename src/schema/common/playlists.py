from typing import ClassVar

from src.i18n.playlist_names import localize_system_playlist_name
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
        is_system = playlist.kind in cls.SYSTEM_KINDS
        extra: dict = {"is_system": is_system}
        if is_system:
            # 系统列表的名字按请求语言替换；自定义列表是用户数据，保持原样。
            extra["name"] = localize_system_playlist_name(playlist.kind, playlist.name)
        return cls.from_peewee_model(playlist, extra=extra)
