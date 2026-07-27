"""订阅影片资源查询的次数状态。

记录每部订阅影片"查过几次资源、上次什么时候查的、还要不要继续查"，由
``SubscribedMovieAutoDownloadService`` 写入，订阅管理页读取。

调度是每天一轮，规则只有两档（配置见 ``settings.downloads.subscription_search_*``）：

- **新片**（``release_date`` 在 ``subscription_search_fresh_days`` 内，含未来日期）：每轮都查，
  **不计次数**，永不放弃。
- **老片**（其余，含 ``release_date`` 为空的——无法证明它新）：每轮都查，``attempt_count`` 累加，
  达到 ``subscription_search_stale_attempt_limit`` 时同一次写入里置 ``exhausted``。

因为放弃与否在写入时就落进了 ``state``，读侧不需要任何时间推导：调度器的候选集是一条纯 SQL
（``state IS NULL OR state != 'exhausted'``），没有 Python 侧的到期筛选。

状态落在通用的 ``ResourceTaskState``（task_key=subscribed_movie_search，resource_type=movie），
不额外建表；``extra`` 一列本任务不使用。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Movie, ResourceTaskState
from src.service.system.resource_task_state_service import ResourceTaskStateService

TASK_KEY = "subscribed_movie_search"
RESOURCE_TYPE = "movie"


def search_state_join_condition():
    """Movie LEFT JOIN 本任务状态行的连接条件，供调度器与订阅管理页共用。"""
    return (
        (ResourceTaskState.resource_id == Movie.id)
        & (ResourceTaskState.task_key == TASK_KEY)
        & (ResourceTaskState.resource_type == RESOURCE_TYPE)
    )


class SubscribedMovieSearchStateService:
    STATE_PENDING = ResourceTaskStateService.STATE_PENDING
    STATE_SUCCEEDED = ResourceTaskStateService.STATE_SUCCEEDED
    STATE_FAILED = ResourceTaskStateService.STATE_FAILED
    STATE_EXHAUSTED = ResourceTaskStateService.STATE_EXHAUSTED

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
    def record_attempt(cls, movie: Movie, *, submitted: bool) -> ResourceTaskState:
        """记一次真正发起过的资源查询。

        提交成功也照常记次数：预算的语义是"我为这片花了几次搜索"。若提交的种子后来判死、影片回到
        候选池，次数继续消耗，跑满同样会被放弃——此时用户能在管理页看到失败的下载任务历史，据此
        决定要不要手动重置。
        """
        now = utc_now_for_db()
        record = ResourceTaskStateService.get_or_create_record(TASK_KEY, movie.id)
        is_fresh = cls.is_fresh(movie, now=now)

        if not is_fresh:
            record.attempt_count += 1

        record.last_attempted_at = now
        record.last_error = None
        if submitted:
            record.state = cls.STATE_SUCCEEDED
            record.last_succeeded_at = now
        elif not is_fresh and record.attempt_count >= cls.stale_attempt_limit():
            record.state = cls.STATE_EXHAUSTED
        else:
            record.state = cls.STATE_PENDING

        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.attempt_count,
                ResourceTaskState.last_attempted_at,
                ResourceTaskState.last_succeeded_at,
                ResourceTaskState.last_error,
            ]
        )
        return record

    @classmethod
    def record_search_error(cls, movie: Movie, detail: str) -> ResourceTaskState:
        """索引器查询本身出错。

        不记 attempt_count、不动 last_attempted_at：索引器故障是运维问题，不该消耗这部影片的
        查询次数，下一轮照常重试。
        """
        now = utc_now_for_db()
        record = ResourceTaskStateService.get_or_create_record(TASK_KEY, movie.id)
        record.state = cls.STATE_FAILED
        record.last_error = detail
        record.last_error_at = now
        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.last_error,
                ResourceTaskState.last_error_at,
            ]
        )
        return record

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
        """重置查询状态：直接删掉状态行，回到"从未查过"，下轮定时任务即会重新查。

        删行而不是把字段逐个拨回默认值——没有状态行本来就是合法的初始态（调度器与管理页都按
        LEFT JOIN 处理空行），少一条需要维护同步的"重置后该长什么样"的规则。

        注意重置**不放开选种黑名单**：黑名单是该影片已判死种子的 info_hash 集合，而 info_hash
        是内容寻址的——同一个 hash 就是同一个 swarm，换索引器它照样是死的。用户重置后真正想要的
        是找一个**别的**种子，黑名单本来就不挡这个。确实想重试某个具体种子时，从 qB 里删掉它即可
        （``DownloadSyncService._prune_ghost_tasks`` 的反向对账会同步删掉本地台账行）。
        """
        if not movie_ids:
            return 0
        return (
            ResourceTaskState.delete()
            .where(
                ResourceTaskState.task_key == TASK_KEY,
                ResourceTaskState.resource_type == RESOURCE_TYPE,
                ResourceTaskState.resource_id.in_(movie_ids),
            )
            .execute()
        )
