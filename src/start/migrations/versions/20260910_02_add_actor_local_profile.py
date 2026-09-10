"""女优本地显示名称和头像覆盖。"""

name = "20260910_02_add_actor_local_profile"


def migrate(database) -> None:
    database.execute_sql(
        """
        ALTER TABLE actor
            ADD COLUMN IF NOT EXISTS display_name_override VARCHAR(255) NULL,
            ADD COLUMN IF NOT EXISTS profile_image_override_id INTEGER NULL
                REFERENCES image(id) ON DELETE SET NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS actor_profile_image_override_id "
        "ON actor (profile_image_override_id)"
    )
