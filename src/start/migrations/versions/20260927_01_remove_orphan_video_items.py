"""删除无媒体的普通视频条目（历史删媒体遗留的幽灵条目）。"""

name = "20260927_01_remove_orphan_video_items"


def migrate(database) -> None:
    tables = set(database.get_tables())
    if "video_item" not in tables or "video_collection_item" not in tables:
        return
    # 非 JAV 条目与媒体一一对应：无媒体的条目已不可播放，连同合集成员一起清理。
    # 时刻的 video_item_id 是无外键的展示快照，随条目删除后仍保留。
    database.execute_sql(
        "DELETE FROM video_collection_item AS link "
        "WHERE NOT EXISTS (SELECT 1 FROM media WHERE media.video_item_id = link.video_item_id)"
    )
    database.execute_sql(
        "DELETE FROM video_item AS item "
        "WHERE NOT EXISTS (SELECT 1 FROM media WHERE media.video_item_id = item.id)"
    )
