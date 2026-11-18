"""持久保存下载提交记录及资源黑名单。"""

name = "20260908_01_add_download_resource_history"


def migrate(database) -> None:
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS download_submission_record (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            client_id INTEGER NOT NULL,
            task_id INTEGER NULL,
            movie_number VARCHAR(255) NOT NULL,
            indexer_name VARCHAR(255) NOT NULL,
            title VARCHAR(255) NOT NULL,
            source_uri TEXT NOT NULL,
            info_hash VARCHAR(40) NOT NULL,
            state VARCHAR(32) NOT NULL,
            remote_id VARCHAR(255) NULL,
            error_code VARCHAR(255) NULL
        )
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS downloadsubmissionrecord_task_id
        ON download_submission_record (task_id)
    """)
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS download_resource_blacklist (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            info_hash VARCHAR(40) NOT NULL
        )
    """)
    database.execute_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS downloadresourceblacklist_info_hash
        ON download_resource_blacklist (info_hash)
    """)
