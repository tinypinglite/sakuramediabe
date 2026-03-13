#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from loguru import logger
from passlib.context import CryptContext

from src.config import settings
from src.model import (
    Actor,
    DownloadClient,
    DownloadTask,
    Image,
    ImageSearchSession,
    ImportJob,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    Tag,
    User,
    UserRefreshToken,
    init_database,
)

def create_tables():
    database = init_database(settings.database)
    database.create_tables(
        [
            User,
            UserRefreshToken,
            Image,
            Tag,
            Actor,
            Movie,
            MovieActor,
            MovieTag,
            MoviePlotImage,
            Playlist,
            PlaylistMovie,
            MediaLibrary,
            Media,
            MediaThumbnail,
            MediaProgress,
            MediaPoint,
            ImageSearchSession,
            DownloadClient,
            DownloadTask,
            ImportJob,
        ],
        safe=True,
    )


def init_user() -> bool:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    hash_password = pwd_context.hash(settings.auth.password)
    username = settings.auth.username
    if User.select().count():
        logger.info("single account already exists, skip init user")
        return False

    User.create(
        username=username,
        password_hash=hash_password,
    )
    return True


def init_system_playlists() -> bool:
    playlist = Playlist.get_or_none(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)
    if playlist is not None:
        logger.info("system playlist already exists, skip init recently played playlist")
        return False

    Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name=RECENTLY_PLAYED_PLAYLIST_NAME,
        description=RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    )
    logger.info("system playlist created kind={}", PLAYLIST_KIND_RECENTLY_PLAYED)
    return True


def initdb():
    logger.info("开始建表...")
    create_tables()
    logger.info("建表完成...")
    logger.info("初始化默认账号...")
    init_user()
    logger.info("默认账号初始化完成...")
    logger.info("初始化系统播放列表...")
    init_system_playlists()
    logger.info("系统播放列表初始化完成...")
    logger.info("所有操作已完成")


if __name__ == "__main__":
    initdb()
