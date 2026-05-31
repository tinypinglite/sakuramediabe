import peewee

from src.model.base import BaseModel
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin

PLAYLIST_KIND_CUSTOM = "custom"
PLAYLIST_KIND_RECENTLY_PLAYED = "recently_played"
PLAYLIST_KIND_VR = "vr"
PLAYLIST_KIND_4K = "4k"
RECENTLY_PLAYED_PLAYLIST_NAME = "最近播放"
RECENTLY_PLAYED_PLAYLIST_DESCRIPTION = "系统自动维护的最近播放影片列表"
VR_PLAYLIST_NAME = "VR"
VR_PLAYLIST_DESCRIPTION = "系统自动维护的 VR 影片列表"
FOUR_K_PLAYLIST_NAME = "4K"
FOUR_K_PLAYLIST_DESCRIPTION = "系统自动维护的 4K 影片列表"

# 系统播放列表的 kind 集合，作为 service / schema 判定"系统列表"的唯一真相源。
SYSTEM_PLAYLIST_KINDS = frozenset(
    {
        PLAYLIST_KIND_RECENTLY_PLAYED,
        PLAYLIST_KIND_VR,
        PLAYLIST_KIND_4K,
    }
)


class Playlist(TimestampedMixin, BaseModel):
    kind = peewee.CharField(max_length=64, default=PLAYLIST_KIND_CUSTOM, index=True)
    name = peewee.CharField(max_length=255, unique=True)
    description = peewee.TextField(default="")

    class Meta:
        table_name = "playlist"


class PlaylistMovie(TimestampedMixin, BaseModel):
    playlist = peewee.ForeignKeyField(Playlist, backref="playlist_movies", on_delete="CASCADE")
    movie = peewee.ForeignKeyField(Movie, backref="playlist_movies", on_delete="CASCADE")

    class Meta:
        table_name = "playlist_movie"
        indexes = ((("playlist", "movie"), True),)
