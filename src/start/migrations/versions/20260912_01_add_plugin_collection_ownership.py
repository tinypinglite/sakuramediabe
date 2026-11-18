"""为插件创建的合集增加稳定 key 和归属。"""

name = "20260912_01_add_plugin_collection_ownership"


def migrate(database) -> None:
    for table, index_name in (
        ("playlist", "playlist_owner_plugin_id_plugin_key"),
        ("moment_collection", "momentcollection_owner_plugin_id_plugin_key"),
        ("clip_collection", "clipcollection_owner_plugin_id_plugin_key"),
    ):
        database.execute_sql(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS owner_plugin_id VARCHAR(64) NULL, "
            "ADD COLUMN IF NOT EXISTS plugin_key VARCHAR(128) NULL"
        )
        database.execute_sql(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {table} (owner_plugin_id, plugin_key)"
        )
