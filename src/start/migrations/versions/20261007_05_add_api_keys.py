"""新增 API 密钥表。

外部集成（如 MCP server）用 Bearer API key 调用 API；服务端只存 SHA-256 哈希，
明文仅在生成响应中出现一次。索引名与模型声明一致：存量库由本迁移建出，
新库由 create_tables 直接建表。
"""

name = "20261007_05_add_api_keys"


def migrate(database) -> None:
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(64) NOT NULL DEFAULT '',
            key_hint VARCHAR(32) NOT NULL,
            key_hash VARCHAR(64) NOT NULL UNIQUE,
            last_used_at TIMESTAMP NULL
        )
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS apikey_key_hash
        ON api_keys (key_hash)
    """)
