"""将 v0.4.21 的任务台账一次收敛为当前 TaskRun 与媒体领域状态。"""

from __future__ import annotations

name = "20260821_01_consolidate_task_runtime"


def _column_exists(database, table_name: str, column_name: str) -> bool:
    return database.table_exists(table_name) and any(
        column.name == column_name for column in database.get_columns(table_name)
    )


def _has_unique_index(database, table_name: str, columns: tuple[str, ...]) -> bool:
    return database.table_exists(table_name) and any(
        index.unique and tuple(index.columns) == columns
        for index in database.get_indexes(table_name)
    )


def _add_notification_identity(database) -> None:
    if not database.table_exists("system_notification"):
        return
    database.execute_sql(
        """
        ALTER TABLE system_notification
          ADD COLUMN IF NOT EXISTS event_type VARCHAR(64) NULL,
          ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(255) NULL,
          ADD COLUMN IF NOT EXISTS resource_type VARCHAR(64) NULL,
          ADD COLUMN IF NOT EXISTS resource_id INTEGER NULL
        """
    )
    if not _has_unique_index(database, "system_notification", ("dedupe_key",)):
        # 历史非空重复键不能静默合并；让迁移失败能迫使运维修正真实冲突。
        database.execute_sql(
            'CREATE UNIQUE INDEX "system_notification_dedupe_key_unique" '
            'ON "system_notification" ("dedupe_key")'
        )


def _add_download_task_columns(database) -> None:
    if not database.table_exists("download_task"):
        return
    database.execute_sql(
        """
        ALTER TABLE download_task
          ADD COLUMN IF NOT EXISTS raw_state VARCHAR(32) NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS download_speed_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS uploaded_speed_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS downloaded_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS total_size_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS eta_seconds INTEGER NULL,
          ADD COLUMN IF NOT EXISTS progress_synced_at TIMESTAMP NULL,
          ADD COLUMN IF NOT EXISTS import_task_run_id INTEGER NULL
        """
    )
    database.execute_sql(
        """
        ALTER TABLE download_task
          ALTER COLUMN raw_state SET DEFAULT '',
          ALTER COLUMN download_speed_bytes SET DEFAULT 0,
          ALTER COLUMN uploaded_speed_bytes SET DEFAULT 0,
          ALTER COLUMN downloaded_bytes SET DEFAULT 0,
          ALTER COLUMN total_size_bytes SET DEFAULT 0
        """
    )
    if database.table_exists("background_task_run"):
        database.execute_sql(
            """
            DO $$ BEGIN
              ALTER TABLE download_task
                ADD CONSTRAINT download_task_import_task_run_id_fkey
                FOREIGN KEY (import_task_run_id) REFERENCES background_task_run(id)
                ON DELETE SET NULL;
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
            """
        )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS download_task_import_task_run_id "
        "ON download_task (import_task_run_id)"
    )
    # v0.4.21 的 running 旧作业没有可恢复的 TaskRun 身份，只能回到幂等导入候选。
    database.execute_sql(
        "UPDATE download_task SET import_status = 'pending' "
        "WHERE import_status = 'running' AND import_task_run_id IS NULL"
    )


def _add_movie_and_thumbnail_state_columns(database) -> None:
    if database.table_exists("movie"):
        database.execute_sql(
            """
            ALTER TABLE movie
              ADD COLUMN IF NOT EXISTS interaction_synced_at TIMESTAMP NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_state VARCHAR(32) NOT NULL DEFAULT 'pending',
              ADD COLUMN IF NOT EXISTS subscription_search_attempt_count INTEGER NOT NULL DEFAULT 0,
              ADD COLUMN IF NOT EXISTS subscription_search_retry_round INTEGER NOT NULL DEFAULT 0,
              ADD COLUMN IF NOT EXISTS subscription_search_last_attempted_at TIMESTAMP NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_last_succeeded_at TIMESTAMP NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_next_retry_at TIMESTAMP NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_error_code VARCHAR(64) NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_last_error TEXT NULL,
              ADD COLUMN IF NOT EXISTS subscription_search_last_error_at TIMESTAMP NULL
            """
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS movie_interaction_synced_at "
            "ON movie (interaction_synced_at)"
        )
        database.execute_sql(
            "CREATE INDEX IF NOT EXISTS movie_subscription_search_state_next_retry_at "
            "ON movie (subscription_search_state, subscription_search_next_retry_at)"
        )

    if not database.table_exists("media"):
        return
    database.execute_sql(
        """
        ALTER TABLE media
          ADD COLUMN IF NOT EXISTS thumbnail_generation_state VARCHAR(32) NOT NULL DEFAULT 'pending',
          ADD COLUMN IF NOT EXISTS thumbnail_attempt_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS thumbnail_deferred_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS thumbnail_next_retry_at TIMESTAMP NULL,
          ADD COLUMN IF NOT EXISTS thumbnail_last_error_code VARCHAR(64) NULL,
          ADD COLUMN IF NOT EXISTS thumbnail_last_error TEXT NULL,
          ADD COLUMN IF NOT EXISTS thumbnail_terminal_at TIMESTAMP NULL,
          ADD COLUMN IF NOT EXISTS thumbnail_source_fingerprint VARCHAR(255) NULL
        """
    )
    # 兼容人工半迁移留下的 NULL，当前模型的新写入仍保持服务端同构默认值。
    database.execute_sql(
        """
        UPDATE media
        SET thumbnail_generation_state = COALESCE(thumbnail_generation_state, 'pending'),
            thumbnail_attempt_count = COALESCE(thumbnail_attempt_count, 0),
            thumbnail_deferred_count = COALESCE(thumbnail_deferred_count, 0)
        WHERE thumbnail_generation_state IS NULL
           OR thumbnail_attempt_count IS NULL
           OR thumbnail_deferred_count IS NULL
        """
    )
    database.execute_sql(
        """
        ALTER TABLE media
          ALTER COLUMN thumbnail_generation_state SET DEFAULT 'pending',
          ALTER COLUMN thumbnail_generation_state SET NOT NULL,
          ALTER COLUMN thumbnail_attempt_count SET DEFAULT 0,
          ALTER COLUMN thumbnail_attempt_count SET NOT NULL,
          ALTER COLUMN thumbnail_deferred_count SET DEFAULT 0,
          ALTER COLUMN thumbnail_deferred_count SET NOT NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS media_thumbnail_generation_state_thumbnail_next_retry_at "
        "ON media (thumbnail_generation_state, thumbnail_next_retry_at)"
    )


def _migrate_resource_task_memory(database) -> None:
    if not database.table_exists("resource_task_state"):
        return

    if (
        database.table_exists("movie")
        and _column_exists(database, "resource_task_state", "last_succeeded_at")
    ):
        # 互动刷新时间是唯一需要保留的通用任务记忆；选择较新的值避免倒退。
        database.execute_sql(
            """
            UPDATE movie AS m
            SET interaction_synced_at = CASE
                WHEN m.interaction_synced_at IS NULL
                  OR s.last_succeeded_at > m.interaction_synced_at
                  THEN s.last_succeeded_at
                ELSE m.interaction_synced_at
            END
            FROM resource_task_state AS s
            WHERE s.task_key = 'movie_interaction_sync'
              AND s.resource_type = 'movie'
              AND s.resource_id = m.id
              AND s.last_succeeded_at IS NOT NULL
            """
        )

    if not database.table_exists("media"):
        return

    state_has_next_retry = _column_exists(
        database, "resource_task_state", "next_retry_at"
    )
    state_has_error_code = _column_exists(
        database, "resource_task_state", "error_code"
    )
    state_has_last_error = _column_exists(
        database, "resource_task_state", "last_error"
    )
    state_has_last_error_at = _column_exists(
        database, "resource_task_state", "last_error_at"
    )
    state_has_attempt_count = _column_exists(
        database, "resource_task_state", "attempt_count"
    )
    if not _column_exists(database, "resource_task_state", "state"):
        return

    thumbnail_exists = (
        "EXISTS (SELECT 1 FROM media_thumbnail AS t WHERE t.media_id = m.id)"
        if database.table_exists("media_thumbnail")
        else "FALSE"
    )
    next_retry = "s.next_retry_at" if state_has_next_retry else "NULL"
    error_code = "s.error_code" if state_has_error_code else "NULL"
    last_error = "s.last_error" if state_has_last_error else "NULL"
    last_error_at = "s.last_error_at" if state_has_last_error_at else "NULL"
    attempt_count = "GREATEST(COALESCE(s.attempt_count, 0), 0)" if state_has_attempt_count else "0"

    # 有实际产物的媒体先标成功；旧台账里留下的失败记录不应覆盖产物事实。
    if database.table_exists("media_thumbnail"):
        database.execute_sql(
            """
            UPDATE media AS m
            SET thumbnail_generation_state = 'succeeded',
                thumbnail_source_fingerprint = m.content_fingerprint,
                thumbnail_attempt_count = 0,
                thumbnail_deferred_count = 0,
                thumbnail_next_retry_at = NULL,
                thumbnail_last_error_code = NULL,
                thumbnail_last_error = NULL,
                thumbnail_terminal_at = NULL
            WHERE EXISTS (
                SELECT 1 FROM media_thumbnail AS t WHERE t.media_id = m.id
            )
            """
        )

    database.execute_sql(
        f"""
        UPDATE media AS m
        SET thumbnail_generation_state = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN 'terminal'
                WHEN s.state = 'succeeded' THEN 'succeeded'
                WHEN s.state = 'failed_retryable' OR {next_retry} IS NOT NULL THEN 'retry_wait'
                ELSE 'pending'
            END,
            thumbnail_attempt_count = {attempt_count},
            thumbnail_deferred_count = 0,
            thumbnail_next_retry_at = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN NULL
                WHEN s.state = 'failed_retryable' OR {next_retry} IS NOT NULL THEN {next_retry}
                ELSE NULL
            END,
            thumbnail_last_error_code = {error_code},
            thumbnail_last_error = {last_error},
            thumbnail_terminal_at = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN {last_error_at}
                ELSE NULL
            END,
            thumbnail_source_fingerprint = m.content_fingerprint
        FROM resource_task_state AS s
        WHERE s.task_key = 'media_thumbnail_generation'
          AND s.resource_type = 'media'
          AND s.resource_id = m.id
          AND NOT {thumbnail_exists}
        """
    )


def _migrate_subscription_search_state(database) -> None:
    """把订阅搜索的可见状态迁入 Movie；尝试历史不再作为运行时依赖。"""
    if not (
        database.table_exists("movie")
        and database.table_exists("resource_task_state")
        and _column_exists(database, "resource_task_state", "state")
    ):
        return

    optional_columns = {
        "attempt_count": "0",
        "retry_round": "0",
        "last_attempted_at": "NULL",
        "last_succeeded_at": "NULL",
        "next_retry_at": "NULL",
        "error_code": "NULL",
        "last_error": "NULL",
        "last_error_at": "NULL",
    }
    values = {
        column: (f"s.{column}" if _column_exists(database, "resource_task_state", column) else fallback)
        for column, fallback in optional_columns.items()
    }
    database.execute_sql(
        f"""
        UPDATE movie AS m
        SET subscription_search_state = CASE
                WHEN s.state IN ('exhausted', 'failed_terminal') THEN 'exhausted'
                WHEN s.state = 'running' THEN 'failed_retryable'
                WHEN s.state = 'succeeded' THEN 'succeeded'
                WHEN s.state = 'failed_retryable' THEN 'failed_retryable'
                ELSE 'pending'
            END,
            subscription_search_attempt_count = GREATEST(COALESCE({values['attempt_count']}, 0), 0),
            subscription_search_retry_round = GREATEST(COALESCE({values['retry_round']}, 0), 0),
            subscription_search_last_attempted_at = {values['last_attempted_at']},
            subscription_search_last_succeeded_at = {values['last_succeeded_at']},
            subscription_search_next_retry_at = {values['next_retry_at']},
            subscription_search_error_code = CASE
                WHEN s.state = 'running' THEN 'task_interrupted'
                ELSE {values['error_code']}
            END,
            subscription_search_last_error = CASE
                WHEN s.state = 'running' THEN '订阅影片资源查询任务中断，等待重试'
                ELSE {values['last_error']}
            END,
            subscription_search_last_error_at = {values['last_error_at']}
        FROM resource_task_state AS s
        WHERE s.task_key = 'subscribed_movie_auto_download'
          AND s.resource_type = 'movie'
          AND s.resource_id = m.id
        """
    )


def _drop_retired_tables(database) -> None:
    # resource_task_state 持有 last_attempt_id 外键，删除顺序必须先 state 后 attempt。
    for table_name in (
        "resource_task_state",
        "resource_task_attempt",
        "system_event",
        "subtitle_import_job",
        "video_import_job",
        "import_job",
    ):
        database.execute_sql(f'DROP TABLE IF EXISTS "{table_name}"')


def migrate(database, migrator) -> None:
    """从 v0.4.21 一次升级到当前任务模型；新库无旧表时也记录为已应用。"""
    _add_notification_identity(database)
    _add_download_task_columns(database)
    _add_movie_and_thumbnail_state_columns(database)
    _migrate_resource_task_memory(database)
    _migrate_subscription_search_state(database)
    _drop_retired_tables(database)
