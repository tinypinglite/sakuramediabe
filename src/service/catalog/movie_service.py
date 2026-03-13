"""影片目录 service。

负责影片列表、详情组装、订阅状态维护，以及按影片编号从 JavDB 拉取并落库。
阅读入口建议从 ``list_movies`` / ``get_movie_detail`` / ``stream_search_and_upsert_movie_from_javdb`` 开始，
再回看查询 helper 和本地资源组装 helper。
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from loguru import logger
from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common import (
    build_signed_media_url,
    build_signed_subtitle_url,
    normalize_movie_number,
    parse_movie_number_from_path,
)
from src.config.config import settings
from src.metadata.gfriends import GfriendsActorImageResolver
from src.metadata.javdb import JavdbProvider
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Tag,
)
from src.model.base import get_database
from src.schema.catalog.movies import (
    MOVIE_LIST_SORT_FIELDS,
    MovieCollectionType,
    MovieActorResource,
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
    MovieMediaSubtitleResource,
    MovieDetailResource,
    MovieListItemResource,
    MovieListStatus,
    MovieNumberParseResponse,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError
from src.service.collections import PlaylistService


class MovieService:
    """聚合 Movie 相关查询、详情拼装和远端导入流程。"""

    MOVIE_LIST_NULLABLE_SORT_FIELDS = {"release_date", "subscribed_at"}
    MOVIE_LIST_SORT_FIELD_MAP = {
        "release_date": Movie.release_date,
        "added_at": Movie.id,
        "subscribed_at": Movie.subscribed_at,
        "comment_count": Movie.comment_count,
        "score_number": Movie.score_number,
        "want_watch_count": Movie.want_watch_count,
        "heat": Movie.heat,
    }

    @staticmethod
    def _playable_exists_expression():
        """返回“影片是否存在可播放媒体”的子查询表达式。"""
        playable_media = Media.select(Media.id).where(
            Media.valid == True,
            Media.movie == Movie.movie_number,
        )
        return fn.EXISTS(playable_media)

    @classmethod
    def _filtered_movies(
        cls,
        actor_id: Optional[int] = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
    ):
        """构建影片列表的基础筛选链路，供列表和计数查询复用。"""
        query = Movie.select()
        if actor_id is None:
            filtered_query = query
        else:
            movie_ids = MovieActor.select(MovieActor.movie).where(MovieActor.actor == actor_id)
            filtered_query = query.where(Movie.id.in_(movie_ids))

        if status == MovieListStatus.SUBSCRIBED:
            filtered_query = filtered_query.where(Movie.is_subscribed == True)
        elif status == MovieListStatus.PLAYABLE:
            filtered_query = filtered_query.where(cls._playable_exists_expression())

        if collection_type == MovieCollectionType.SINGLE:
            filtered_query = filtered_query.where(Movie.is_collection == False)
        return filtered_query

    @classmethod
    def _build_movie_list_sort(cls, sort: Optional[str]) -> Sequence:
        """解析 ``field:direction`` 排序表达式，并补上稳定的次级排序。"""
        if sort is None:
            return [Movie.movie_number.asc()]

        normalized = sort.strip().lower()
        if not normalized:
            return [Movie.movie_number.asc()]

        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError:
            raise ApiError(
                422,
                "invalid_movie_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        if field_name not in MOVIE_LIST_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ApiError(
                422,
                "invalid_movie_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        sort_field = cls.MOVIE_LIST_SORT_FIELD_MAP[field_name]
        ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = Movie.id.asc() if direction == "asc" else Movie.id.desc()
        if field_name in cls.MOVIE_LIST_NULLABLE_SORT_FIELDS:
            # 允许空值的字段统一放到后面，避免不同数据库里空值排序行为不一致。
            return [sort_field.is_null(), ordered_field, tie_breaker]
        return [ordered_field, tie_breaker]

    @classmethod
    def _movie_list_query(
        cls,
        actor_id: Optional[int] = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        sort: Optional[str] = None,
    ):
        """列表查询统一在这里补齐封面图和 ``can_play`` 计算列。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        return (
            cls._filtered_movies(actor_id, status, collection_type)
            .select(Movie, Image, can_play_expression)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .order_by(*cls._build_movie_list_sort(sort))
        )

    @classmethod
    def _latest_movies_query(cls):
        """按最近导入媒体时间倒序列出影片，而不是按影片自身创建时间。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        latest_media_created_at = fn.MAX(Media.created_at)
        return (
            Movie.select(Movie, Image, can_play_expression)
            .join(Media)
            .switch(Movie)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .group_by(Movie.id, Image.id)
            .order_by(latest_media_created_at.desc(), Movie.id.desc())
        )

    @staticmethod
    def _normalized_movie_number_expression():
        """把库内编号归一化成和搜索输入一致的比较格式。"""
        normalized = fn.UPPER(fn.TRIM(Movie.movie_number))
        normalized = fn.REPLACE(normalized, " ", "")
        normalized = fn.REPLACE(normalized, "_", "-")
        normalized = fn.REPLACE(normalized, "PPV-", "")
        return normalized

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        metadata_proxy = (settings.metadata.proxy or "").strip() or None
        actor_image_resolver = GfriendsActorImageResolver(
            filetree_url=settings.metadata.gfriends_filetree_url,
            cdn_base_url=settings.metadata.gfriends_cdn_base_url,
            cache_path=settings.metadata.gfriends_filetree_cache_path,
            cache_ttl_hours=settings.metadata.gfriends_filetree_cache_ttl_hours,
            proxy=metadata_proxy,
        )
        return JavdbProvider(
            host=settings.metadata.javdb_host,
            proxy=metadata_proxy,
            actor_image_resolver=actor_image_resolver,
        )

    @classmethod
    def _build_catalog_import_service(cls) -> CatalogImportService:
        return CatalogImportService()

    @staticmethod
    def _require_movie(movie_number: str) -> Movie:
        movie = Movie.get_or_none(Movie.movie_number == movie_number)
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})
        return movie

    @staticmethod
    def _list_movie_media(movie: Movie) -> List[Media]:
        return list(
            Media.select(Media)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )

    @staticmethod
    def _delete_local_media_files(media_items: List[Media]) -> None:
        for media in media_items:
            try:
                Path(media.path).unlink()
            except FileNotFoundError:
                continue

    @staticmethod
    def _actors(movie: Movie) -> List[Actor]:
        return list(
            Actor.select(Actor, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
            .join(MovieActor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .where(MovieActor.movie == movie)
            .order_by(Actor.id)
        )

    @staticmethod
    def _plot_images(movie: Movie) -> List[Image]:
        query = (
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        return [link.image for link in query]

    @staticmethod
    def _media_items(movie: Movie) -> List[MovieMediaResource]:
        """把媒体、播放进度和打点信息折叠成详情页需要的资源结构。"""
        media_items = list(
            Media.select(Media)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )
        if not media_items:
            return []

        media_ids = [media.id for media in media_items]
        # 进度和打点分开查，避免在一个大 join 里把媒体行放大成笛卡尔展开。
        progress_items = {
            progress.media_id: progress
            for progress in MediaProgress.select(MediaProgress).where(MediaProgress.media.in_(media_ids))
        }

        points_by_media_id: Dict[int, List[MovieMediaPointResource]] = {}
        point_query = (
            MediaPoint.select(MediaPoint)
            .where(MediaPoint.media.in_(media_ids))
            .order_by(MediaPoint.media, MediaPoint.id)
        )
        for point in point_query:
            if point.media_id not in points_by_media_id:
                points_by_media_id[point.media_id] = []
            points_by_media_id[point.media_id].append(
                MovieMediaPointResource(
                    point_id=point.id,
                    offset_seconds=point.offset_seconds,
                )
            )

        resources: List[MovieMediaResource] = []
        for media in media_items:
            # 详情资源需要把播放进度和精彩时间点挂回各自 media 上。
            progress = progress_items.get(media.id)
            if progress is None:
                media.progress = None
            else:
                media.progress = MovieMediaProgressResource(
                    last_position_seconds=progress.position_seconds,
                    last_watched_at=progress.last_watched_at,
                )
            media.points = points_by_media_id.get(media.id, [])
            media.play_url = build_signed_media_url(media.id)
            media.subtitles = MovieService._subtitle_items(media)
            resources.append(MovieMediaResource.from_attributes_model(media))
        return resources

    @staticmethod
    def _subtitle_items(media: Media) -> List[MovieMediaSubtitleResource]:
        if not media.valid:
            return []

        media_path = Path(media.path).expanduser()
        if not media_path.exists() or not media_path.is_file():
            return []

        media_directory = media_path.parent
        if not media_directory.exists() or not media_directory.is_dir():
            return []

        subtitles: List[MovieMediaSubtitleResource] = []
        for subtitle_path in sorted(media_directory.iterdir(), key=lambda path: path.name.lower()):
            if not subtitle_path.is_file() or subtitle_path.suffix.lower() != ".srt":
                continue
            subtitles.append(
                MovieMediaSubtitleResource(
                    file_name=subtitle_path.name,
                    url=build_signed_subtitle_url(media.id, subtitle_path.name),
                )
            )
        return subtitles

    @staticmethod
    def get_movie_detail(movie_number: str) -> MovieDetailResource:
        """组装影片详情页所需的所有关联资源。"""
        thin_cover_alias = Image.alias()
        movie = (
            Movie.select(Movie, Image, thin_cover_alias)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .switch(Movie)
            .join(
                thin_cover_alias,
                JOIN.LEFT_OUTER,
                on=(Movie.thin_cover_image == thin_cover_alias.id),
                attr="thin_cover_image",
            )
            .where(Movie.movie_number == movie_number)
            .get_or_none()
        )
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        # 标签、演员、剧照、媒体都按详情页独立查询，避免一次 join 带来重复行和复杂去重。
        tags = [
            TagResource(tag_id=tag.id, name=tag.name)
            for tag in Tag.select(Tag).join(MovieTag).where(MovieTag.movie == movie).order_by(Tag.id)
        ]

        movie.actors = MovieService._actors(movie)
        movie.tags = tags
        movie.plot_images = MovieService._plot_images(movie)
        movie.media_items = MovieService._media_items(movie)
        movie.playlists = PlaylistService.list_movie_playlists(movie)
        movie.can_play = any(media_item.valid for media_item in movie.media_items)
        return MovieDetailResource.from_attributes_model(movie)

    @staticmethod
    def list_movies(
        actor_id: Optional[int] = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        sort: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = MovieService._filtered_movies(actor_id, status, collection_type).count()
        movies = list(
            MovieService._movie_list_query(actor_id, status, collection_type, sort).offset(start).limit(page_size)
        )
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def list_latest_movies(
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = Movie.select(Movie.id).join(Media).group_by(Movie.id).count()
        movies = list(MovieService._latest_movies_query().offset(start).limit(page_size))
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def parse_movie_number_query(query: str) -> MovieNumberParseResponse:
        parsed_movie_number = parse_movie_number_from_path(query.strip())
        if not parsed_movie_number:
            return MovieNumberParseResponse(
                query=query,
                parsed=False,
                movie_number=None,
                reason="movie_number_not_found",
            )
        return MovieNumberParseResponse(
            query=query,
            parsed=True,
            movie_number=parsed_movie_number,
            reason=None,
        )

    @classmethod
    def search_local_movies(cls, movie_number: str) -> List[MovieListItemResource]:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            return []
        # 本地搜索只取最匹配的一条，职责是回答“库里有没有这个番号”。
        movies = list(
            cls._movie_list_query().where(
                cls._normalized_movie_number_expression() == normalized_movie_number
            ).limit(1)
        )
        return MovieListItemResource.from_items(movies)

    @classmethod
    def set_subscription(cls, movie_number: str, subscribed: bool) -> None:
        movie = cls._require_movie(movie_number)
        was_subscribed = bool(movie.is_subscribed)
        movie.is_subscribed = subscribed
        if subscribed:
            if not was_subscribed or movie.subscribed_at is None:
                movie.subscribed_at = datetime.utcnow()
        else:
            movie.subscribed_at = None
        movie.save()

    @classmethod
    def unsubscribe_movie(cls, movie_number: str, delete_media: bool = False) -> None:
        movie = cls._require_movie(movie_number)
        media_items = cls._list_movie_media(movie)
        # 已有本地媒体时默认阻止取消订阅，避免把“停止追踪影片”和“删除本地资源”混成一个动作。
        if media_items and not delete_media:
            raise ApiError(
                409,
                "movie_subscription_has_media",
                "影片存在媒体文件，若需取消订阅请传 delete_media=true",
                {
                    "movie_number": movie_number,
                    "media_count": len(media_items),
                    "delete_media_required": True,
                },
            )

        if media_items and delete_media:
            # 取消订阅并不会物理删除 Media 记录，而是把现有记录标记为 invalid，保留导入历史。
            cls._delete_local_media_files(media_items)
            with get_database().atomic():
                movie.is_subscribed = False
                movie.subscribed_at = None
                movie.save()
                (
                    Media.update(valid=False)
                    .where(Media.movie == movie)
                    .execute()
                )
            return

        movie.is_subscribed = False
        movie.subscribed_at = None
        movie.save()

    @classmethod
    def stream_search_and_upsert_movie_from_javdb(
        cls,
        movie_number: str,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出影片搜索和导入进度。"""
        normalized_movie_number = normalize_movie_number(movie_number)
        yield "search_started", {"movie_number": normalized_movie_number}

        if not normalized_movie_number:
            yield "completed", {"success": False, "reason": "movie_number_not_found", "movies": []}
            return

        try:
            detail = cls._build_javdb_provider().get_movie_by_number(normalized_movie_number)
        except MetadataNotFoundError:
            yield "completed", {"success": False, "reason": "movie_not_found", "movies": []}
            return
        except Exception as exc:
            logger.exception(
                "Javdb movie search failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            yield "completed", {"success": False, "reason": "internal_error", "movies": []}
            return

        # 先把搜索命中的原始远端信息回给前端，再开始实际落库。
        yield "movie_found", {
            "movies": [
                {
                    "javdb_id": detail.javdb_id,
                    "movie_number": detail.movie_number,
                    "title": detail.title,
                    "cover_image": detail.cover_image,
                }
            ],
            "total": 1,
        }
        yield "upsert_started", {"total": 1}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        failed_items: List[Dict[str, str]] = []
        imported_movies: List[MovieListItemResource] = []
        stats = {
            "total": 1,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }

        existed_before_upsert = Movie.get_or_none(
            (Movie.movie_number == detail.movie_number) | (Movie.javdb_id == detail.javdb_id)
        ) is not None
        try:
            # upsert 成功后重新走列表查询，确保响应里带上封面和 can_play 等派生字段。
            movie = cls._build_catalog_import_service().upsert_movie_from_javdb_detail(detail)
            movie_with_cover = cls._movie_list_query().where(Movie.id == movie.id).get_or_none() or movie
            imported_movies.append(MovieListItemResource.from_attributes_model(movie_with_cover))
            if existed_before_upsert:
                already_exists_count += 1
            else:
                created_count += 1
        except ImageDownloadError as exc:
            failed_count += 1
            logger.warning(
                "Javdb movie image download failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            failed_items.append(
                {
                    "movie_number": normalized_movie_number,
                    "reason": "image_download_failed",
                    "detail": str(exc),
                }
            )
        except Exception as exc:
            failed_count += 1
            logger.exception(
                "Javdb movie upsert failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            failed_items.append(
                {
                    "movie_number": normalized_movie_number,
                    "reason": "upsert_failed",
                    "detail": str(exc),
                }
            )

        stats["created_count"] = created_count
        stats["already_exists_count"] = already_exists_count
        stats["failed_count"] = failed_count
        yield "upsert_finished", stats

        if imported_movies:
            yield "completed", {
                "success": True,
                "movies": [movie_item.model_dump(exclude={"can_play"}) for movie_item in imported_movies],
                "failed_items": failed_items,
                "stats": stats,
            }
            return

        yield "completed", {
            "success": False,
            "reason": "internal_error",
            "movies": [],
            "failed_items": failed_items,
            "stats": stats,
        }
