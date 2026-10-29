"""新增全局自动下载选种策略。

存量库写入与旧自动下载逻辑一致的默认值；新库由 initdb 写入新一代默认排序。
"""

import json

name = "20261030_01_add_download_selection_policy"


def migrate(database) -> None:
    database.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS download_selection_policy (
            id SERIAL PRIMARY KEY,
            rules JSONB NOT NULL DEFAULT '{}'::jsonb,
            version BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            CONSTRAINT download_selection_policy_singleton CHECK (id = 1)
        )
        """
    )
    legacy_rules = {
        "schema_version": 1,
        "indexer_kinds": [],
        "indexer_names": [],
        "min_size_bytes": 1024**3,
        "max_size_bytes": 40 * 1024**3,
        "min_seeders": 1,
        "max_age_days": None,
        "match_title": True,
        "match_description": True,
        "required_groups": [],
        "excluded_terms": [],
        "preferred_groups": [],
        "ranking": ["size_desc"],
        "indexer_priority_names": [],
    }
    database.execute_sql(
        """
        INSERT INTO download_selection_policy (
            id, rules, version, created_at, updated_at
        ) VALUES (1, %s::jsonb, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (json.dumps(legacy_rules, ensure_ascii=False),),
    )
