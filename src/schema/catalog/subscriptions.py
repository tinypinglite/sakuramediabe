"""影片订阅管理协议。

订阅管理页要展示的是"这部订阅片的资源查询走到哪一步了"，字段构成和普通影片列表差别很大，
因此独立成一套 schema，不去污染 ``MovieListItemResource``（它被所有影片列表接口共用）。
"""

from datetime import date, datetime
from enum import Enum

from pydantic import field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel


class MovieSubscriptionStatus(str, Enum):
    """订阅影片的资源状态。

    ALL 之外的七项由 ``MovieSubscriptionService._status_expression()`` 这**一个** SQL CASE
    表达式判定，筛选 / 计数 / 列表展示共用它，所以枚举顺序即优先级、七项严格互斥、计数之和
    恒等于订阅总数。
    """

    ALL = "all"
    # 已入库：本地已有 Media。订阅继续保留——订阅是长期意图，不因为下到了就自动解除。
    IMPORTED = "imported"
    # 下载中：有活跃下载任务（failed / abandoned / stalled_dead 的任务不算）且其导入还在途
    # （import_status=pending/running）。"下完了正等自动导入"也归这里——那是秒级过渡态，用户
    # 对它的动作和真下载中一样（等着），不值得再切一个状态出来。
    DOWNLOADING = "downloading"
    # 导入失败：有活跃下载任务，但没有一个还在途——导入这一趟已经跑完，库里却没有 Media。
    # 除了 import_status=failed，也包含"跑完了零产出"（如整包只有小于阈值的样本文件，扫描记
    # skipped、任务落 completed）。文件已经在盘上，卡的是入库这一步，重下没有意义。
    IMPORT_FAILED = "import_failed"
    # 已放弃：老片连续找不到资源达到上限，等待用户重开预算。
    EXHAUSTED = "exhausted"
    # 查询出错：索引器或下载提交链路故障，不消耗“未找到”预算。
    FAILED = "failed"
    # 缺资源：查过但没有可用候选，下一轮仍会继续搜索。
    MISSING = "missing"
    # 待查：订阅后尚未进入过资源查询。
    PENDING = "pending"


class MovieSubscriptionSort(str, Enum):
    SUBSCRIBED_AT_DESC = "subscribed_at:desc"
    SUBSCRIBED_AT_ASC = "subscribed_at:asc"
    RELEASE_DATE_DESC = "release_date:desc"
    RELEASE_DATE_ASC = "release_date:asc"
    LAST_SEARCHED_AT_DESC = "last_searched_at:desc"
    LAST_SEARCHED_AT_ASC = "last_searched_at:asc"
    ATTEMPT_COUNT_DESC = "attempt_count:desc"


class MovieSubscriptionListItemResource(SchemaModel):
    movie_id: int
    movie_number: str
    title: str
    cover_image: ImageResource | None = None
    release_date: str | None = None
    subscribed_at: datetime | None = None
    status: MovieSubscriptionStatus
    is_fresh: bool = False
    attempt_count: int = 0
    attempt_limit: int = 0
    last_searched_at: datetime | None = None
    last_error: str | None = None
    # 该影片已判死的下载任务数：试过几个种子都失败了。
    dead_download_task_count: int = 0
    media_count: int = 0

    @field_validator("release_date", mode="before")
    @classmethod
    def serialize_release_date(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value


class MovieSubscriptionStatusCountsResource(SchemaModel):
    total: int = 0
    imported: int = 0
    downloading: int = 0
    import_failed: int = 0
    pending: int = 0
    missing: int = 0
    exhausted: int = 0
    failed: int = 0


class MovieSubscriptionSearchResetRequest(SchemaModel):
    """省略 movie_ids 时重开全部已放弃订阅；传入时只重开指定影片。"""

    movie_ids: list[int] | None = None


class MovieSubscriptionSearchResetResponse(SchemaModel):
    reset_count: int
