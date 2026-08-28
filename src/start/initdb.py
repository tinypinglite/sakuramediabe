import bcrypt
from loguru import logger

from src.config import settings
from src.model import (
    PLAYLIST_KIND_RECENTLY_PLAYED,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    Actor,
    BackgroundTaskRun,
    ClipCollection,
    ClipCollectionItem,
    DailyRecommendationItem,
    DownloadClient,
    DownloadTask,
    Image,
    ImageSearchIndexState,
    ImageSearchSession,
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
    MovieTag,
    Playlist,
    PlaylistMovie,
    RankingItem,
    SchemaMigration,
    Subtitle,
    SystemNotification,
    Tag,
    User,
    UserRefreshToken,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
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
            ImageSearchIndexState,
            ImageSearchSession,
            RankingItem,
            DailyRecommendationItem,
            BackgroundTaskRun,
            SchemaMigration,
            SystemNotification,
            DownloadClient,
            Indexer,
            IndexerDownloadClient,
            DownloadTask,
        ],
        safe=True,
    )
    return database


def init_user() -> bool:
    # 直接用 bcrypt 库生成口令哈希（passlib 已移除；bcrypt 默认 rounds=12 与
    # 既有 passlib 生成的 $2b$ 哈希完全兼容，存量密码可继续校验）。
    hash_password = bcrypt.hashpw(
        settings.auth.password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")
    username = settings.auth.username
    if User.select().count():
        logger.info("single account already exists, skip init user")
        return False

    User.create(
        username=username,
        password_hash=hash_password,
    )
    return True


# 系统播放列表预置清单：最近播放成员物化存储，由播放进度上报维护。
SYSTEM_PLAYLIST_SPECS = (
    (PLAYLIST_KIND_RECENTLY_PLAYED, RECENTLY_PLAYED_PLAYLIST_NAME, RECENTLY_PLAYED_PLAYLIST_DESCRIPTION),
)


def init_system_playlists() -> bool:
    """逐个幂等预置系统播放列表。"""
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
