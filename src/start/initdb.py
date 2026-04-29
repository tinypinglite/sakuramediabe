#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from loguru import logger
from passlib.context import CryptContext

from src.config import settings
from src.model import (
    Actor,
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    HotReviewItem,
    Image,
    ImageSearchSession,
    ImportJob,
    Indexer,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieSimilarity,
    MovieTag,
    Playlist,
    PlaylistMovie,
    RankingItem,
    ResourceTaskState,
    SchemaMigration,
    Subtitle,
    SystemEvent,
    SystemNotification,
    Tag,
    User,
    UserRefreshToken,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    init_database,
)


def create_tables():
    database = init_database(settings.database)
    # 新版只以当前 Peewee 模型为准，不再尝试修复旧库结构或迁移历史字段。
    database.create_tables(
        [
            User,
            UserRefreshToken,
            Image,
            Tag,
            Actor,
            MovieSeries,
            Movie,
            MovieActor,
            MovieTag,
            MoviePlotImage,
            Subtitle,
            Playlist,
            PlaylistMovie,
            MediaLibrary,
            Media,
            MediaThumbnail,
            MediaProgress,
            MediaPoint,
            ImageSearchSession,
            RankingItem,
            HotReviewItem,
            MovieSimilarity,
            BackgroundTaskRun,
            ResourceTaskState,
            SchemaMigration,
            SystemNotification,
            SystemEvent,
            DownloadClient,
            Indexer,
            DownloadTask,
            ImportJob,
        ],
        safe=True,
    )
    return database


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
