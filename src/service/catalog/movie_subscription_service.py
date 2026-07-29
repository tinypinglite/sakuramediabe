"""影片订阅管理 service。

订阅是长期意图标记：影片入库之后订阅照常保留，不会自动解除。因此订阅列表只增不减，默认视图应该
是"需要关注的"（缺资源 / 已放弃 / 查询出错），全部订阅放次要位置。

两件事：
- 列出订阅影片及其资源查询状态（筛选、排序、分页、计数全部在 SQL 侧完成）
- 重置查询状态：删掉状态行让影片重新参与自动查询

批量取消订阅不在这里：不删文件的走已有的 ``POST /movies/unsubscriptions``，要删媒体文件的走
``DELETE /media/{media_id}``，本域不再平行造一套。
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from peewee import JOIN, Case, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import media_exists_expression, validate_page
from src.model import DownloadTask, Image, Media, Movie, ResourceTaskState
from src.schema.catalog.subscriptions import (
    MovieSubscriptionImportOperationResource,
    MovieSubscriptionListItemResource,
    MovieSubscriptionSearchResetResponse,
    MovieSubscriptionSort,
    MovieSubscriptionStatus,
    MovieSubscriptionStatusCountsResource,
)
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.service.transfers.common import (
    active_download_task_exists_expression,
    unfinished_import_download_task_exists_expression,
    download_task_dead_expression,
)
from src.service.transfers.subscribed_movie_search_state_service import (
    ERROR_CODE_NO_CANDIDATE,
    RESOURCE_TYPE as SEARCH_RESOURCE_TYPE,
    TASK_KEY as SEARCH_TASK_KEY,
    SubscribedMovieSearchStateService,
    search_state_join_condition,
)

# 这些 transfers 域导入可以写在模块级，前提是 transfers 侧一律**从子模块而非 src.service.catalog
# 包**反向导入（见 media_import_service.py 顶部注释）。一旦哪天有人把那边改回 `from
# src.service.catalog import X`，catalog <-> transfers 的包级循环就会立刻在这里炸成
# ImportError: cannot import name ... from partially initialized module。


class MovieSubscriptionService:
    SORT_EXPRESSIONS = {
        MovieSubscriptionSort.SUBSCRIBED_AT_DESC: (Movie.subscribed_at, "desc"),
        MovieSubscriptionSort.SUBSCRIBED_AT_ASC: (Movie.subscribed_at, "asc"),
        MovieSubscriptionSort.RELEASE_DATE_DESC: (Movie.release_date, "desc"),
        MovieSubscriptionSort.RELEASE_DATE_ASC: (Movie.release_date, "asc"),
        MovieSubscriptionSort.LAST_SEARCHED_AT_DESC: (ResourceTaskState.last_attempted_at, "desc"),
        MovieSubscriptionSort.LAST_SEARCHED_AT_ASC: (ResourceTaskState.last_attempted_at, "asc"),
        MovieSubscriptionSort.ATTEMPT_COUNT_DESC: (ResourceTaskState.attempt_count, "desc"),
    }

    # ------------------------------------------------------------------ 查询

    @classmethod
    def _status_expression(cls):
        """订阅状态的**唯一定义**，求值为 MovieSubscriptionStatus 的字符串值。

        筛选、计数、列表展示三处共用同一个表达式，因此不存在"SQL 一套判定、Python 再抄一套"
        的漂移风险（此前正是两份实现，靠注释约束一致）。

        互斥性由 CASE 的分支顺序天然保证，各状态计数之和恒等于订阅总数这一性质自动成立。
        另外 CASE 是短路求值的：每行最多只会跑到三个 EXISTS 子查询，不会把七个分支的子查询
        全部展开（此前 count_by_status 为每个状态各建一个 SUM(CASE)，每行 11 个相关子查询）。
        """
        return Case(
            None,
            (
                (media_exists_expression(), MovieSubscriptionStatus.IMPORTED.value),
                # 下面两个分支把"有活跃下载任务"这一集合二分：先命中"导入还在途"的算下载中，
                # 剩下的活跃任务就是"导入这趟已经跑完、库里却没有 Media"，即卡在入库。
                # 顺序不能反：DOWNLOADING 是 IMPORT_FAILED 所在超集的真子集，放后面会被吞掉。
                # 两者并集恒等于"有活跃任务"——这条不变量必须守住，搜索闸门读的是同一个集合。
                (
                    unfinished_import_download_task_exists_expression(),
                    MovieSubscriptionStatus.DOWNLOADING.value,
                ),
                (
                    active_download_task_exists_expression(),
                    MovieSubscriptionStatus.IMPORT_FAILED.value,
                ),
                (
                    ResourceTaskState.state == SubscribedMovieSearchStateService.STATE_EXHAUSTED,
                    MovieSubscriptionStatus.EXHAUSTED.value,
                ),
                (
                    # kernel 记账后失败落 failed_retryable；「查过没找到」有专属 error_code，
                    # 仍归 MISSING 档（走下面 last_attempted_at 分支），只有真故障才亮 FAILED。
                    (
                        ResourceTaskState.state
                        == SubscribedMovieSearchStateService.STATE_FAILED_RETRYABLE
                    )
                    & (
                        ResourceTaskState.error_code.is_null(True)
                        | (ResourceTaskState.error_code != ERROR_CODE_NO_CANDIDATE)
                    ),
                    MovieSubscriptionStatus.FAILED.value,
                ),
                (
                    ResourceTaskState.last_attempted_at.is_null(False),
                    MovieSubscriptionStatus.MISSING.value,
                ),
            ),
            MovieSubscriptionStatus.PENDING.value,
        )

    @classmethod
    def _base_query(cls, *selections):
        """所有订阅影片 LEFT JOIN 资源查询状态行。

        LEFT JOIN 是必须的：已入库的订阅片从来不参与资源查询，压根没有状态行。
        """
        return (
            Movie.select(*selections)
            .join(ResourceTaskState, JOIN.LEFT_OUTER, on=search_state_join_condition())
            .where(Movie.is_subscribed == True)
        )

    @classmethod
    def _resolve_sort(cls, sort: MovieSubscriptionSort) -> list:
        sort_field, direction = cls.SORT_EXPRESSIONS[sort]
        ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = Movie.id.asc() if direction == "asc" else Movie.id.desc()
        # 这几个字段都允许为空（未订阅时间、无发布日期、从未查过），空值统一排到最后。
        return [sort_field.is_null(), ordered_field, tie_breaker]

    @classmethod
    def list_subscriptions(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        status: MovieSubscriptionStatus = MovieSubscriptionStatus.ALL,
        sort: MovieSubscriptionSort = MovieSubscriptionSort.SUBSCRIBED_AT_DESC,
        search: str | None = None,
    ) -> PageResponse[MovieSubscriptionListItemResource]:
        validate_page(page, page_size, error_code="invalid_movie_subscription_filter")
        now = utc_now_for_db()

        status_expression = cls._status_expression()
        query = cls._base_query(Movie.id, status_expression.alias("status"))
        if status != MovieSubscriptionStatus.ALL:
            # Postgres 不允许 WHERE 引用 SELECT 别名，表达式重复渲染一次即可。
            query = query.where(status_expression == status.value)

        normalized_search = (search or "").strip()
        if normalized_search:
            keyword = f"%{normalized_search}%"
            query = query.where(
                (Movie.movie_number ** keyword)
                | (Movie.title ** keyword)
                | (Movie.title_zh ** keyword)
            )

        total = query.count()
        start = (page - 1) * page_size
        rows = list(
            query.order_by(*cls._resolve_sort(sort)).offset(start).limit(page_size).tuples()
        )
        status_by_movie_id = {row[0]: row[1] for row in rows}
        return PageResponse[MovieSubscriptionListItemResource](
            items=cls._build_items(
                [row[0] for row in rows], status_by_movie_id=status_by_movie_id, now=now
            ),
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def count_by_status(cls) -> MovieSubscriptionStatusCountsResource:
        """一次 GROUP BY 算齐各状态计数，避免每个 tab 打一次 COUNT。"""
        status_expression = cls._status_expression()
        rows = (
            cls._base_query(status_expression.alias("status"), fn.COUNT(Movie.id))
            .group_by(status_expression)
            .tuples()
        )
        counts = {status_value: int(total or 0) for status_value, total in rows}
        return MovieSubscriptionStatusCountsResource(
            total=sum(counts.values()),
            **{
                status.value: counts.get(status.value, 0)
                for status in MovieSubscriptionStatus
                if status != MovieSubscriptionStatus.ALL
            },
        )

    @classmethod
    def _build_items(
        cls,
        movie_ids: list[int],
        *,
        status_by_movie_id: dict[int, str],
        now: datetime,
    ) -> list[MovieSubscriptionListItemResource]:
        if not movie_ids:
            return []
        movies = {movie.id: movie for movie in Movie.select().where(Movie.id.in_(movie_ids))}
        records = SubscribedMovieSearchStateService.load_records(movie_ids)
        # 保持分页查询给出的顺序：字典按 id 取回会丢掉排序。
        ordered_movies = [movies[movie_id] for movie_id in movie_ids if movie_id in movies]
        cover_images = cls._load_cover_images(ordered_movies)
        movie_numbers = [movie.movie_number for movie in ordered_movies]
        # media_exists_expression 用的是精确相等，这里的计数必须同样精确匹配才和状态判定一致。
        media_counts = cls._count_by_key(
            Media.select(Media.movie, fn.COUNT(Media.id))
            .where(Media.movie.in_(movie_numbers))
            .group_by(Media.movie)
        )
        dead_task_counts = cls._count_dead_download_tasks(movie_numbers)
        attempt_limit = SubscribedMovieSearchStateService.stale_attempt_limit()
        # 只为 import_failed 档取导入作业上下文：其余档没有可操作的导入语义。
        import_failed_numbers = [
            movie.movie_number
            for movie in ordered_movies
            if status_by_movie_id[movie.id] == MovieSubscriptionStatus.IMPORT_FAILED.value
        ]
        import_operations = cls._load_latest_import_operations(import_failed_numbers)

        items: list[MovieSubscriptionListItemResource] = []
        for movie in ordered_movies:
            record = records.get(movie.id)
            items.append(
                MovieSubscriptionListItemResource(
                    movie_number=movie.movie_number,
                    title=movie.title,
                    title_zh=movie.title_zh or "",
                    cover_image=cover_images.get(movie.cover_image_id),
                    release_date=movie.release_date,
                    subscribed_at=movie.subscribed_at,
                    status=status_by_movie_id[movie.id],
                    is_fresh=SubscribedMovieSearchStateService.is_fresh(movie, now=now),
                    attempt_count=record.attempt_count if record else 0,
                    attempt_limit=attempt_limit,
                    last_searched_at=record.last_attempted_at if record else None,
                    last_error=record.last_error if record else None,
                    dead_download_task_count=dead_task_counts.get(movie.movie_number, 0),
                    media_count=media_counts.get(movie.movie_number, 0),
                    import_operation=import_operations.get(movie.movie_number),
                )
            )
        return items

    @classmethod
    def _load_latest_import_operations(
        cls, movie_numbers: list[str]
    ) -> dict[str, MovieSubscriptionImportOperationResource]:
        """当页 import_failed 影片的最新导入作业上下文，一次 IN 查询完成。

        通路：Movie.movie_number ==（裸列，双侧索引）== DownloadTask.movie_number
        → ImportJob.download_task（外键）。「最新」= 影片维度 ImportJob.id 最大。
        failed_files 的 kind 判定只能在 Python 侧解析（JSON 列），当页 ≤20 条无往返放大；
        绝不把 ImportJob 引入 _status_expression 的 CASE——状态七态互斥的不变量不许破坏。
        """
        if not movie_numbers:
            return {}
        import json

        from src.common.media_import_status import TERMINAL_JOB_STATES
        from src.model import ImportJob
        from src.service.transfers.base_import_job_service import BaseImportJobService

        rows = (
            ImportJob.select(
                ImportJob.id,
                ImportJob.state,
                ImportJob.imported_count,
                ImportJob.skipped_count,
                ImportJob.failed_count,
                ImportJob.failed_files,
                DownloadTask.movie,
            )
            .join(DownloadTask, on=(ImportJob.download_task == DownloadTask.id))
            .where(DownloadTask.movie.in_(movie_numbers))
            .order_by(DownloadTask.movie, ImportJob.id.desc())
            .tuples()
        )
        operations: dict[str, MovieSubscriptionImportOperationResource] = {}
        for job_id, state, imported, skipped, failed, failed_files, movie_number in rows:
            if movie_number in operations:
                continue
            try:
                failure_items = json.loads(failed_files) if failed_files else []
            except (TypeError, ValueError):
                failure_items = []
            retryable_file_count = sum(
                1
                for item in failure_items
                if isinstance(item, dict)
                and BaseImportJobService._entry_kind(item) == "file"
            )
            available_actions = ["open_import_job"]
            if state in TERMINAL_JOB_STATES:
                if retryable_file_count:
                    available_actions.append("retry_failed_files")
                available_actions.append("rerun_import")
            operations[movie_number] = MovieSubscriptionImportOperationResource(
                import_job_id=job_id,
                state=state,
                imported_count=imported,
                skipped_count=skipped,
                failed_count=failed,
                retryable_file_count=retryable_file_count,
                available_actions=available_actions,
            )
        return operations

    @staticmethod
    def _count_by_key(query) -> dict[str, int]:
        return {row[0]: int(row[1]) for row in query.tuples()}

    @classmethod
    def _count_dead_download_tasks(cls, movie_numbers: list[str]) -> dict[str, int]:
        """按番号统计已判死的下载任务数——"这片试过几个种子都死了"。

        入参来自本页 Movie 行的规范番号，download_task.movie_number 由提交链路拷贝同一列，
        两侧直接裸列精确比较即可；套 UPPER(TRIM()) 只会废掉该列索引。
        """
        counts_by_key = cls._count_by_key(
            DownloadTask.select(DownloadTask.movie, fn.COUNT(DownloadTask.id))
            .where(DownloadTask.movie.in_(movie_numbers))
            .where(download_task_dead_expression())
            .group_by(DownloadTask.movie)
        )
        return {number: counts_by_key.get(number, 0) for number in movie_numbers}

    @staticmethod
    def _load_cover_images(movies: list[Movie]) -> dict[int, ImageResource]:
        """一次取齐当页封面，避免逐条访问 movie.cover_image 触发 N+1。"""
        image_ids = {movie.cover_image_id for movie in movies if movie.cover_image_id}
        if not image_ids:
            return {}
        return {
            image.id: ImageResource.from_peewee_model(image)
            for image in Image.select().where(Image.id.in_(list(image_ids)))
        }

    # ------------------------------------------------------------------ 重置

    @classmethod
    def reset_search_state(
        cls, *, movie_numbers: list[str], reset_all_exhausted: bool
    ) -> MovieSubscriptionSearchResetResponse:
        """重置资源查询状态：删掉状态行，下轮定时任务即会重新查。

        重置不放开选种黑名单——理由见 SubscribedMovieSearchStateService.reset 的说明。
        """
        movie_ids = cls._resolve_reset_targets(
            movie_numbers=movie_numbers, reset_all_exhausted=reset_all_exhausted
        )
        reset_count = SubscribedMovieSearchStateService.reset(movie_ids)
        logger.info(
            "Subscription search state reset movies={} deleted_rows={}",
            len(movie_ids),
            reset_count,
        )
        return MovieSubscriptionSearchResetResponse(reset_count=reset_count)

    @classmethod
    def _resolve_reset_targets(
        cls, *, movie_numbers: list[str], reset_all_exhausted: bool
    ) -> list[int]:
        if reset_all_exhausted:
            exhausted_ids = ResourceTaskState.select(ResourceTaskState.resource_id).where(
                ResourceTaskState.task_key == SEARCH_TASK_KEY,
                ResourceTaskState.resource_type == SEARCH_RESOURCE_TYPE,
                ResourceTaskState.state == SubscribedMovieSearchStateService.STATE_EXHAUSTED,
            )
            return [
                row[0]
                for row in Movie.select(Movie.id)
                .where(Movie.is_subscribed == True, Movie.id.in_(exhausted_ids))
                .tuples()
            ]

        deduped_numbers = cls._dedup_movie_numbers(movie_numbers)
        if not deduped_numbers:
            raise ApiError(
                422,
                "invalid_movie_subscription_reset",
                "需要指定 movie_numbers 或把 reset_all_exhausted 置为 true",
            )
        return [
            row[0]
            for row in Movie.select(Movie.id)
            .where(
                Movie.is_subscribed == True,
                Movie.movie_number.in_(deduped_numbers),
            )
            .tuples()
        ]

    @staticmethod
    def _dedup_movie_numbers(movie_numbers: list[str]) -> list[str]:
        # 入参来自管理页展示的规范番号，只去空白去重、精确匹配即可，不做归一化改写。
        deduped_numbers: list[str] = []
        for movie_number in movie_numbers:
            stripped = (movie_number or "").strip()
            if stripped and stripped not in deduped_numbers:
                deduped_numbers.append(stripped)
        return deduped_numbers
