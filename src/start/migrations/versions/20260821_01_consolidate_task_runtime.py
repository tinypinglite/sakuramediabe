"""把 v0.4.21 的旧任务台账收敛为 v0.5.0 当前模型。"""

from __future__ import annotations

name = "20260821_01_consolidate_task_runtime"


def migrate(database) -> None:
    """只接收 v0.4.21 schema；新库由当前模型直接建表。"""
    # 通知是展示缓存，不参与业务状态；删除后重新建立事件身份约束。
    database.execute_sql("DELETE FROM system_notification")
    database.execute_sql(
        """
        ALTER TABLE system_notification
          ADD COLUMN event_type VARCHAR(64) NULL,
          ADD COLUMN dedupe_key VARCHAR(255) NULL,
          ADD COLUMN resource_type VARCHAR(64) NULL,
          ADD COLUMN resource_id INTEGER NULL
        """
    )
    database.execute_sql(
        'CREATE UNIQUE INDEX "system_notification_dedupe_key_unique" '
        'ON "system_notification" ("dedupe_key")'
    )

    database.execute_sql(
        """
        ALTER TABLE download_task
          ADD COLUMN raw_state VARCHAR(32) NOT NULL DEFAULT '',
          ADD COLUMN download_speed_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN uploaded_speed_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN downloaded_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN total_size_bytes BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN eta_seconds INTEGER NULL,
          ADD COLUMN progress_synced_at TIMESTAMP NULL,
          ADD COLUMN import_task_run_id INTEGER NULL
        """
    )
    database.execute_sql(
        """
        ALTER TABLE download_task
          ADD CONSTRAINT download_task_import_task_run_id_fkey
          FOREIGN KEY (import_task_run_id) REFERENCES background_task_run(id)
          ON DELETE SET NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX download_task_import_task_run_id "
        "ON download_task (import_task_run_id)"
    )
    database.execute_sql(
        "UPDATE download_task SET import_status = 'pending' "
        "WHERE import_status = 'running'"
    )

    database.execute_sql(
        """
        ALTER TABLE movie
          ADD COLUMN interaction_synced_at TIMESTAMP NULL,
          ADD COLUMN subscription_search_state VARCHAR(32) NOT NULL DEFAULT 'pending',
          ADD COLUMN subscription_search_attempt_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN subscription_search_retry_round INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN subscription_search_last_attempted_at TIMESTAMP NULL,
          ADD COLUMN subscription_search_last_succeeded_at TIMESTAMP NULL,
          ADD COLUMN subscription_search_next_retry_at TIMESTAMP NULL,
          ADD COLUMN subscription_search_error_code VARCHAR(64) NULL,
          ADD COLUMN subscription_search_last_error TEXT NULL,
          ADD COLUMN subscription_search_last_error_at TIMESTAMP NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX movie_interaction_synced_at ON movie (interaction_synced_at)"
    )
    database.execute_sql(
        "CREATE INDEX movie_subscription_search_state_next_retry_at "
        "ON movie (subscription_search_state, subscription_search_next_retry_at)"
    )

    database.execute_sql(
        """
        ALTER TABLE media
          ADD COLUMN thumbnail_generation_state VARCHAR(32) NOT NULL DEFAULT 'pending',
          ADD COLUMN thumbnail_attempt_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN thumbnail_deferred_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN thumbnail_next_retry_at TIMESTAMP NULL,
          ADD COLUMN thumbnail_last_error_code VARCHAR(64) NULL,
          ADD COLUMN thumbnail_last_error TEXT NULL,
          ADD COLUMN thumbnail_terminal_at TIMESTAMP NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX media_thumbnail_generation_state_thumbnail_next_retry_at "
        "ON media (thumbnail_generation_state, thumbnail_next_retry_at)"
    )

    # 互动成功时间、缩略图状态和订阅搜索状态从旧通用台账迁到领域表。
    database.execute_sql(
        """
        UPDATE movie AS m
        SET interaction_synced_at = s.last_succeeded_at
        FROM resource_task_state AS s
        WHERE s.task_key = 'movie_interaction_sync'
          AND s.resource_type = 'movie'
          AND s.resource_id = m.id
          AND s.last_succeeded_at IS NOT NULL
        """
    )
    database.execute_sql(
        """
        UPDATE media AS m
        SET thumbnail_generation_state = 'succeeded',
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
        """
        UPDATE media AS m
        SET thumbnail_generation_state = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN 'terminal'
                WHEN s.state = 'succeeded' THEN 'succeeded'
                WHEN s.state = 'failed_retryable' OR s.next_retry_at IS NOT NULL THEN 'retry_wait'
                ELSE 'pending'
            END,
            thumbnail_attempt_count = GREATEST(COALESCE(s.attempt_count, 0), 0),
            thumbnail_deferred_count = 0,
            thumbnail_next_retry_at = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN NULL
                WHEN s.state = 'failed_retryable' OR s.next_retry_at IS NOT NULL THEN s.next_retry_at
                ELSE NULL
            END,
            thumbnail_last_error_code = s.error_code,
            thumbnail_last_error = s.last_error,
            thumbnail_terminal_at = CASE
                WHEN s.state IN ('failed_terminal', 'exhausted') THEN s.last_error_at
                ELSE NULL
            END
        FROM resource_task_state AS s
        WHERE s.task_key = 'media_thumbnail_generation'
          AND s.resource_type = 'media'
          AND s.resource_id = m.id
          AND NOT EXISTS (
              SELECT 1 FROM media_thumbnail AS t WHERE t.media_id = m.id
          )
        """
    )
    database.execute_sql(
        """
        UPDATE movie AS m
        SET subscription_search_state = CASE
                WHEN s.state IN ('exhausted', 'failed_terminal') THEN 'exhausted'
                WHEN s.state = 'running' THEN 'failed_retryable'
                WHEN s.state = 'succeeded' THEN 'succeeded'
                WHEN s.state = 'failed_retryable' THEN 'failed_retryable'
                ELSE 'pending'
            END,
            subscription_search_attempt_count = GREATEST(COALESCE(s.attempt_count, 0), 0),
            subscription_search_retry_round = GREATEST(COALESCE(s.retry_round, 0), 0),
            subscription_search_last_attempted_at = s.last_attempted_at,
            subscription_search_last_succeeded_at = s.last_succeeded_at,
            subscription_search_next_retry_at = s.next_retry_at,
            subscription_search_error_code = CASE
                WHEN s.state = 'running' THEN 'task_interrupted'
                ELSE s.error_code
            END,
            subscription_search_last_error = CASE
                WHEN s.state = 'running' THEN '订阅影片资源查询任务中断，等待重试'
                ELSE s.last_error
            END,
            subscription_search_last_error_at = s.last_error_at
        FROM resource_task_state AS s
        WHERE s.task_key = 'subscribed_movie_auto_download'
          AND s.resource_type = 'movie'
          AND s.resource_id = m.id
        """
    )

    # v0.5.0 不再恢复旧进程内任务：保留历史记录，但终止活动行并释放互斥锁。
    database.execute_sql(
        """
        UPDATE background_task_run
        SET state = 'failed',
            finished_at = CURRENT_TIMESTAMP,
            mutex_key = NULL,
            error_message = 'v0.5.0 任务运行时已重置'
        WHERE state IN ('pending', 'running')
        """
    )
    database.execute_sql("ALTER TABLE background_task_run DROP COLUMN owner_pid")

    for table_name in (
        "resource_task_state",
        "resource_task_attempt",
        "system_event",
        "subtitle_import_job",
        "video_import_job",
        "import_job",
    ):
        database.execute_sql(f'DROP TABLE IF EXISTS "{table_name}"')
