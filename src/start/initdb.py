#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from loguru import logger
from passlib.context import CryptContext

from src.config import settings
from src.model import (
    Actor,
    BackgroundTaskRun,
    ClipCollection,
    ClipCollectionItem,
    DailyRecommendationItem,
    DownloadClient,
    DownloadTask,
    HotReviewItem,
    Image,
    ImageSearchSession,
    ImportJob,
    Indexer,
    IndexerDownloadClient,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    MomentRecommendation,
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
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
    VideoItem,
    User,
    UserRefreshToken,
    FOUR_K_PLAYLIST_DESCRIPTION,
    FOUR_K_PLAYLIST_NAME,
    PLAYLIST_KIND_4K,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_VR,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    VR_PLAYLIST_DESCRIPTION,
    VR_PLAYLIST_NAME,
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
            VideoItem,
            VideoCollection,
            VideoCollectionItem,
            Playlist,
            PlaylistMovie,
            MediaLibrary,
            Media,
            MediaThumbnail,
            MediaProgress,
            MediaPoint,
            MediaClip,
            ClipCollection,
            ClipCollectionItem,
            MomentRecommendation,
            ImageSearchSession,
            RankingItem,
            HotReviewItem,
            MovieSimilarity,
            DailyRecommendationItem,
            BackgroundTaskRun,
            ResourceTaskState,
            SchemaMigration,
            SystemNotification,
            SystemEvent,
            DownloadClient,
            Indexer,
            IndexerDownloadClient,
            DownloadTask,
            ImportJob,
            VideoImportJob,
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


# 系统播放列表预置清单：最近播放为物化维护，VR/4K 为按 special_tags 实时派生的虚拟列表。
SYSTEM_PLAYLIST_SPECS = (
    (PLAYLIST_KIND_RECENTLY_PLAYED, RECENTLY_PLAYED_PLAYLIST_NAME, RECENTLY_PLAYED_PLAYLIST_DESCRIPTION),
    (PLAYLIST_KIND_VR, VR_PLAYLIST_NAME, VR_PLAYLIST_DESCRIPTION),
    (PLAYLIST_KIND_4K, FOUR_K_PLAYLIST_NAME, FOUR_K_PLAYLIST_DESCRIPTION),
)


def init_system_playlists() -> bool:
    """逐个幂等预置系统播放列表；老库升级时会自动补建缺失的 VR/4K。"""
    created_any = False
    for kind, name, description in SYSTEM_PLAYLIST_SPECS:
        if Playlist.get_or_none(Playlist.kind == kind) is not None:
            continue
        Playlist.create(kind=kind, name=name, description=description)
        logger.info("system playlist created kind={}", kind)
        created_any = True
    if not created_any:
        logger.info("system playlists already exist, skip init")
    return created_any


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
