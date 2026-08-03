"""订阅影片资源查询的状态读取与重置（已迁 kernel 记账，任务架构 Wave 2）。

写入侧已收敛到 ``ResourceTaskRunner``（``SubscribedMovieAutoDownloadService`` 的
process_one），本模块只保留读侧与重置：

- task_key 与 job 合并为 ``subscribed_movie_auto_download``（原 ``subscribed_movie_search``
  已随迁移清空，两套 key 的历史包袱就此了结）；
- 新片（``release_date`` 在 ``subscription_search_fresh_days`` 内）走 RetryPolicy 的
  预算豁免：失败照记、不计次数、永不 exhausted；
- 老片跑满 ``subscription_search_stale_attempt_limit`` 次"查过没找到"后 exhausted；
- 索引器/提交故障声明 ``consumes_budget=False``：落错误信息但不吃预算；
- 重置从"删行"改为"重开预算"（retry_round+1、清投影快照），尝试历史保留在 attempt 表。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Movie, ResourceTaskState

TASK_KEY = "subscribed_movie_auto_download"
RESOURCE_TYPE = "movie"

# "查过但没有可用资源"的结构化错误码：订阅页据此把它展示为「未找到」而非「查询失败」。
ERROR_CODE_NO_CANDIDATE = "no_candidate_found"


def search_state_join_condition():
    """Movie LEFT JOIN 本任务状态行的连接条件，供调度器与订阅管理页共用。"""
    return (
        (ResourceTaskState.resource_id == Movie.id)
        & (ResourceTaskState.task_key == TASK_KEY)
        & (ResourceTaskState.resource_type == RESOURCE_TYPE)
    )


class SubscribedMovieSearchStateService:
    STATE_PENDING = "pending"
    STATE_RUNNING = "running"
    STATE_SUCCEEDED = "succeeded"
    STATE_FAILED_RETRYABLE = "failed_retryable"
    STATE_EXHAUSTED = "exhausted"

    @staticmethod
    def stale_attempt_limit() -> int:
        return settings.downloads.subscription_search_stale_attempt_limit

    @staticmethod
    def is_fresh(movie: Movie, *, now: datetime) -> bool:
        """影片是否算新片。

        ``release_date`` 为空一律按老片：无法证明它新，就不该享受"永不放弃"的待遇。
        未来日期天然落在窗口内（``> now - fresh_days``），未上映的预订片按新片处理。
        """
        release_date = movie.release_date
        if release_date is None:
            return False
        fresh_days = settings.downloads.subscription_search_fresh_days
        return release_date > now - timedelta(days=fresh_days)

    @classmethod
    def load_records(cls, movie_ids: list[int]) -> dict[int, ResourceTaskState]:
        """按影片 id 批量取状态行，供列表展示避免 N+1。"""
        if not movie_ids:
            return {}
        query = ResourceTaskState.select().where(
            ResourceTaskState.task_key == TASK_KEY,
            ResourceTaskState.resource_type == RESOURCE_TYPE,
            ResourceTaskState.resource_id.in_(movie_ids),
        )
        return {record.resource_id: record for record in query}

    @classmethod
    def reset(cls, movie_ids: list[int]) -> int:
        """重置查询状态 = 重开预算：轮次 +1、投影快照清回"从未查过"的样子。

        不再删行（旧语义）——尝试历史保留在 attempt 表，投影字段拨回初始态即可让
        调度器下轮重新纳入、订阅页回到 pending 档。

        注意重置**不放开选种黑名单**：黑名单是该影片已判死种子的 info_hash 集合，而
        info_hash 是内容寻址的——同一个 hash 就是同一个 swarm，换索引器它照样是死的。
        确实想重试某个具体种子时，从 qB 里删掉它即可。
        """
        if not movie_ids:
            return 0
        now = utc_now_for_db()
        return (
            ResourceTaskState.update(
                state=cls.STATE_PENDING,
                attempt_count=0,
                retry_round=ResourceTaskState.retry_round + 1,
                last_attempted_at=None,
                last_succeeded_at=None,
                last_error=None,
                last_error_at=None,
                error_code=None,
                next_retry_at=None,
                updated_at=now,
            )
            .where(
                ResourceTaskState.task_key == TASK_KEY,
                ResourceTaskState.resource_type == RESOURCE_TYPE,
                ResourceTaskState.resource_id.in_(movie_ids),
            )
            .execute()
        )
