"""影片订阅管理 service。

订阅是长期意图标记：影片入库之后订阅照常保留，不会自动解除。

本服务列出订阅影片及其领域状态，筛选、排序、分页、计数全部在 SQL 侧完成。

批量取消订阅不在这里：不删文件的走已有的 ``POST /movies/unsubscriptions``，要删媒体文件的走
``DELETE /media/{media_id}``，本域不再平行造一套。
"""

from __future__ import annotations

from peewee import Case, fn

from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    build_ordered_expressions,
    count_by_owner,
    media_exists_expression,
    validate_page,
)
from src.model import DownloadTask, Image, Media, Movie
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.subscriptions import (
    MovieSubscriptionListItemResource,
    MovieSubscriptionSort,
    MovieSubscriptionStatus,
    MovieSubscriptionStatusCountsResource,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog.movie_subscription_search_state_service import (
    ERROR_CODE_NO_CANDIDATE,
    STATE_EXHAUSTED,
    STATE_FAILED_RETRYABLE,
    MovieSubscriptionSearchStateService,
)
from src.service.transfers.shared.common import (
    active_download_task_exists_expression,
    unfinished_import_download_task_exists_expression,
)

# 这些 transfers 域导入可以写在模块级，前提是 transfers 侧一律**从子模块而非 src.service.catalog
# 包**反向导入。一旦哪天有人把那边改回 `from src.service.catalog import X`，
# catalog <-> transfers 的包级循环就会立刻在这里炸成
# ImportError: cannot import name ... from partially initialized module。


class MovieSubscriptionService:
    SORT_EXPRESSIONS = {
        MovieSubscriptionSort.SUBSCRIBED_AT_DESC: (Movie.subscribed_at, "desc"),
        MovieSubscriptionSort.SUBSCRIBED_AT_ASC: (Movie.subscribed_at, "asc"),
        MovieSubscriptionSort.RELEASE_DATE_DESC: (Movie.release_date, "desc"),
        MovieSubscriptionSort.RELEASE_DATE_ASC: (Movie.release_date, "asc"),
        MovieSubscriptionSort.LAST_SEARCHED_AT_DESC: (
            Movie.subscription_search_last_attempted_at,
            "desc",
        ),
        MovieSubscriptionSort.LAST_SEARCHED_AT_ASC: (
            Movie.subscription_search_last_attempted_at,
            "asc",
        ),
        MovieSubscriptionSort.ATTEMPT_COUNT_DESC: (
            Movie.subscription_search_attempt_count,
            "desc",
        ),
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
                    Movie.subscription_search_state == STATE_EXHAUSTED,
                    MovieSubscriptionStatus.EXHAUSTED.value,
                ),
                (
                    (Movie.subscription_search_state == STATE_FAILED_RETRYABLE)
                    & (
                        Movie.subscription_search_error_code.is_null(True)
                        | (
                            Movie.subscription_search_error_code
                            != ERROR_CODE_NO_CANDIDATE
                        )
                    ),
                    MovieSubscriptionStatus.FAILED.value,
                ),
                (
                    Movie.subscription_search_last_attempted_at.is_null(False),
                    MovieSubscriptionStatus.MISSING.value,
                ),
            ),
            MovieSubscriptionStatus.PENDING.value,
        )

    @classmethod
    def _base_query(cls, *selections):
        """所有订阅影片；展示状态只从媒体与下载领域事实推导。"""
        return Movie.select(*selections).where(Movie.is_subscribed == True)

    @classmethod
    def _resolve_sort(cls, sort: MovieSubscriptionSort) -> list:
        sort_field, direction = cls.SORT_EXPRESSIONS[sort]
        # 这几个字段都允许为空（未订阅时间、无发布日期、从未查过），空值统一排到最后。
        return build_ordered_expressions(
            sort_field,
            direction,
            nullable=True,
            tie_breaker=Movie.id,
        )

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
            query = query.where((Movie.movie_number**keyword) | (Movie.title**keyword))

        total = query.count()
        start = (page - 1) * page_size
        rows = list(
            query.order_by(*cls._resolve_sort(sort))
            .offset(start)
            .limit(page_size)
            .tuples()
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
        now,
    ) -> list[MovieSubscriptionListItemResource]:
        if not movie_ids:
            return []
        movies = {
            movie.id: movie for movie in Movie.select().where(Movie.id.in_(movie_ids))
        }
        # 保持分页查询给出的顺序：字典按 id 取回会丢掉排序。
        ordered_movies = [
            movies[movie_id] for movie_id in movie_ids if movie_id in movies
        ]
        cover_images = cls._load_cover_images(ordered_movies)
        movie_numbers = [movie.movie_number for movie in ordered_movies]
        # media_exists_expression 用的是精确相等，这里的计数必须同样精确匹配才和状态判定一致。
        media_counts = count_by_owner(Media, Media.movie, movie_numbers)
        failed_task_counts = cls._count_failed_download_tasks(movie_numbers)
        attempt_limit = MovieSubscriptionSearchStateService.stale_attempt_limit()
        items: list[MovieSubscriptionListItemResource] = []
        for movie in ordered_movies:
            items.append(
                MovieSubscriptionListItemResource(
                    movie_id=movie.id,
                    movie_number=movie.movie_number,
                    title=movie.title,
                    cover_image=cover_images.get(movie.cover_image_id),
                    release_date=movie.release_date,
                    subscribed_at=movie.subscribed_at,
                    status=status_by_movie_id[movie.id],
                    is_fresh=MovieSubscriptionSearchStateService.is_fresh(movie, now=now),
                    attempt_count=movie.subscription_search_attempt_count,
                    attempt_limit=attempt_limit,
                    last_searched_at=movie.subscription_search_last_attempted_at,
                    last_error=movie.subscription_search_last_error,
                    dead_download_task_count=failed_task_counts.get(
                        movie.movie_number, 0
                    ),
                    media_count=media_counts.get(movie.movie_number, 0),
                )
            )
        return items

    @classmethod
    def _count_failed_download_tasks(cls, movie_numbers: list[str]) -> dict[str, int]:
        """统计下载提供方明确失败的历史任务。"""
        counts_by_key = count_by_owner(
            DownloadTask,
            DownloadTask.movie,
            movie_numbers,
            DownloadTask.state == "failed",
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
