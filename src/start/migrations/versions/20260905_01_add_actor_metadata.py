"""演员插件资料与字段归属；历史记录保持空资料。"""

name = "20260905_01_add_actor_metadata"


def migrate(database) -> None:
    database.execute_sql(
        """
        ALTER TABLE actor
            ADD COLUMN IF NOT EXISTS birthday DATE NULL,
            ADD COLUMN IF NOT EXISTS height_cm INTEGER NULL,
            ADD COLUMN IF NOT EXISTS bust_cm INTEGER NULL,
            ADD COLUMN IF NOT EXISTS waist_cm INTEGER NULL,
            ADD COLUMN IF NOT EXISTS hips_cm INTEGER NULL,
            ADD COLUMN IF NOT EXISTS cup VARCHAR(255) NULL,
            ADD COLUMN IF NOT EXISTS birthplace VARCHAR(255) NULL,
            ADD COLUMN IF NOT EXISTS blood_type VARCHAR(255) NULL,
            ADD COLUMN IF NOT EXISTS field_owners JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS mutation_revision BIGINT NOT NULL DEFAULT 0
        """
    )
