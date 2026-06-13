from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import (
    BackgroundTaskRun,
    Media,
    MediaLibrary,
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
    VideoItem,
)
from src.start.migrations import SkipMigration

name = "20260613_01_add_videos_and_decouple_media"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _column_nullable(database, *, table_name: str, column_name: str) -> bool:
    for column in database.get_columns(table_name):
        if column.name == column_name:
            return column.null
    return False


def migrate(database, migrator) -> None:
    if not database.table_exists("media"):
        # media 表尚未建出时不能误记迁移完成，留待建表后再放松约束。
        raise SkipMigration("media table does not exist")

    # 1) 先用 Peewee 模型补建 videos 域新表，保证字段类型与当前方言一致。
    #    video_import_job 承接非 JAV 视频的异步目录导入作业（源路径/媒体库/导入模式/统计/失败文件）。
    videos_models = [
        VideoItem,
        VideoCollection,
        VideoCollectionItem,
        VideoImportJob,
    ]
    # 绑定被外键引用但已存在的表（media_library / background_task_run），保证 FK DDL 解析正确；
    # create_tables(safe=True) 只补建缺失的 videos 表，不会重建既有表。
    bind_models = videos_models + [MediaLibrary, BackgroundTaskRun]
    with database.bind_ctx(bind_models, bind_refs=False, bind_backrefs=False):
        database.create_tables(videos_models, safe=True)

    # 2) Media 新增 video_item 外键列（可空），承接非 JAV 文件归属。
    #    直接复用 Media 模型上已绑定的外键字段，保证 rel_field 与列类型与当前方言一致。
    if not _column_exists(database, table_name="media", column_name="video_item_id"):
        run_migration(
            migrator.add_column(
                "media",
                "video_item_id",
                Media._meta.fields["video_item"],
            )
        )

    # 3) 把原本必填的 movie_number 外键列放松为可空，使 Media 可只归属 video_item。
    if not _column_nullable(database, table_name="media", column_name="movie_number"):
        run_migration(migrator.drop_not_null("media", "movie_number"))
