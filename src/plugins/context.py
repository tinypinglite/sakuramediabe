"""插件访问宿主能力的稳定门面。

具体 service/provider 在方法内懒导入，避免插件加载阶段反向依赖任务注册表。
插件只允许使用本类方法与 ``src.plugins.types`` 中的公开类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.plugins.types import (
    ACTOR_SNAPSHOT_FIELDS,
    MOVIE_SNAPSHOT_FIELDS,
    ActorPage,
    ActorSnapshot,
    MoviePage,
    MovieSnapshot,
    SubtitleAsset,
    SubtitleContent,
    TagSnapshot,
)


class ActorApi:
    """context.actors：演员只读快照与资料 patch，不暴露 ORM。"""

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @staticmethod
    def _to_snapshot(actor) -> ActorSnapshot:
        return ActorSnapshot(
            actor_id=actor.id,
            revision=actor.mutation_revision,
            values=MappingProxyType({name: getattr(actor, name) for name in ACTOR_SNAPSHOT_FIELDS}),
            owners=MappingProxyType(dict(actor.field_owners or {})),
        )

    def get(self, actor_id: int) -> ActorSnapshot | None:
        from src.model import Actor

        actor = Actor.get_or_none(Actor.id == actor_id)
        return self._to_snapshot(actor) if actor is not None else None

    def list_page(self, *, after_id: int = 0, limit: int = 500) -> ActorPage:
        if type(after_id) is not int or after_id < 0:
            raise ValueError("after_id 必须是非负整数")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        from src.model import Actor

        rows = list(Actor.select().where(Actor.id > after_id).order_by(Actor.id).limit(limit + 1))
        page = rows[:limit]
        return ActorPage(
            items=tuple(self._to_snapshot(actor) for actor in page),
            next_cursor=page[-1].id if len(rows) > limit else None,
        )

    def patch(self, actor_id: int, fields: dict[str, Any], expected_revision: int) -> bool:
        from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway

        return ActorOwnershipGateway.patch_plugin(actor_id, self._plugin_id, fields, expected_revision)


class MovieApi:
    """``context.movies``：影片只读快照与受保护字段 patch（v2-lite 契约 v2）。

    读取和 patch 都不暴露 ORM 对象；写入只经 MovieOwnershipGateway，插件拿不到
    任何可写句柄。字段 owner 与 revision 语义见 v2-lite 设计文档第 3/4 节。
    """

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @classmethod
    def _to_snapshot(cls, movie) -> MovieSnapshot:
        return cls._to_snapshots([movie])[0]

    @staticmethod
    def _to_snapshots(movies) -> list[MovieSnapshot]:
        from src.model import Actor, MovieActor, MovieSeries, MovieTag, Tag

        if not movies:
            return []
        movie_ids = [movie.id for movie in movies]
        actors = {movie_id: [] for movie_id in movie_ids}
        tags = {movie_id: [] for movie_id in movie_ids}
        # 一页内批量读取关联，避免每部影片/每位演员单独查询。
        for link in (
            MovieActor.select(MovieActor, Actor).join(Actor)
            .where(MovieActor.movie.in_(movie_ids))
            .order_by(MovieActor.movie, MovieActor.actor)
        ):
            actors[link.movie_id].append(ActorApi._to_snapshot(link.actor))
        for link in (
            MovieTag.select(MovieTag, Tag).join(Tag)
            .where(MovieTag.movie.in_(movie_ids))
            .order_by(MovieTag.movie, MovieTag.tag)
        ):
            tags[link.movie_id].append(TagSnapshot(tag_id=link.tag_id, name=link.tag.name))
        series_ids = {movie.series_id for movie in movies if movie.series_id is not None}
        series_names = {
            series.id: series.name
            for series in MovieSeries.select().where(MovieSeries.id.in_(series_ids))
        } if series_ids else {}
        return [
            MovieSnapshot(
                movie_id=movie.id,
                revision=movie.mutation_revision,
                values=MappingProxyType({
                    name: series_names.get(movie.series_id) if name == "series_name"
                    else getattr(movie, name)
                    for name in MOVIE_SNAPSHOT_FIELDS
                }),
                owners=MappingProxyType(dict(movie.field_owners or {})),
                actors=tuple(actors[movie.id]),
                tags=tuple(tags[movie.id]),
            )
            for movie in movies
        ]

    def get(self, movie_id: int) -> MovieSnapshot | None:
        """按内部 id 读取影片快照；不存在返回 None。"""
        from src.model import Movie

        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            return None
        return self._to_snapshot(movie)

    def find_by_numbers(self, numbers) -> list[MovieSnapshot]:
        """按番号批量查找（大小写不敏感 + 分隔符候选，与人工输入点查同语义）。

        结果按输入顺序去重返回；找不到的番号跳过。
        """
        from src.common.service_helpers import find_movie_by_number

        movies = []
        seen_ids: set[int] = set()
        for number in numbers:
            movie = find_movie_by_number(number)
            if movie is None or movie.id in seen_ids:
                continue
            seen_ids.add(movie.id)
            movies.append(movie)
        return self._to_snapshots(movies)

    def list_page(self, *, after_id: int = 0, limit: int = 500) -> MoviePage:
        """按 Movie.id 游标分页遍历全库，返回不可变影片快照。"""
        if after_id < 0:
            raise ValueError("after_id 不能小于 0")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")

        from src.model import Movie

        rows = list(
            Movie.select()
            .where(Movie.id > after_id)
            .order_by(Movie.id)
            .limit(limit + 1)
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return MoviePage(
            items=tuple(self._to_snapshots(page_rows)),
            next_cursor=page_rows[-1].id if has_more else None,
        )

    def patch(
        self,
        movie_id: int,
        fields: dict[str, Any],
        expected_revision: int,
    ) -> bool:
        """写受保护字段（白名单内）并取得/续持 owner，乐观并发提交。

        不存在、revision 不匹配、字段由人工/其他插件持有，或试图屏蔽已订阅影片时，
        返回 False 且整次零修改；插件应重新读取 snapshot 后决定是否重试。
        """
        from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

        return MovieOwnershipGateway.patch_plugin(
            movie_id,
            self._plugin_id,
            fields,
            expected_revision,
        )


class SubtitleApi:
    """``context.subtitles``：已登记影片字幕的只读访问。"""

    @staticmethod
    def list(movie_id: int) -> tuple[SubtitleAsset, ...]:
        """列出仍可访问的字幕；影片不存在时抛 SubtitleReadError。"""
        from src.service.catalog.movie_subtitle_service import MovieSubtitleService

        return MovieSubtitleService.list_subtitle_assets(movie_id)

    @staticmethod
    def read(movie_id: int, subtitle_id: int) -> SubtitleContent:
        """读取属于该影片的字幕，最多 10 MiB；失败抛 SubtitleReadError。"""
        from src.service.catalog.movie_subtitle_service import MovieSubtitleService

        return MovieSubtitleService.read_subtitle_content(movie_id, subtitle_id)


@dataclass(frozen=True, init=False)
class PluginContext:
    """插件上下文：配置只读、数据目录归插件所有、宿主能力按方法暴露。"""

    plugin_id: str
    settings: Mapping[str, Any]
    _data_dir: Path

    def __init__(self, plugin_id: str, settings: Mapping[str, Any], data_dir: Path):
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "_data_dir", Path(data_dir))

    def ensure_data_dir(self) -> Path:
        """确保插件数据目录存在并返回（``<root>/<plugin_id>/data``）。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    @property
    def data_dir(self) -> Path:
        return self.ensure_data_dir()

    @property
    def actors(self) -> ActorApi:
        """演员资料读写；身份及订阅字段只读。"""
        return ActorApi(self.plugin_id)

    @property
    def movies(self) -> MovieApi:
        """影片只读快照与受保护字段写入出口（v2-lite 契约 v2）。"""
        return MovieApi(self.plugin_id)

    @property
    def subtitles(self) -> SubtitleApi:
        """字幕元信息、原始字节和内容指纹；不提供可写句柄。"""
        return SubtitleApi()

    @staticmethod
    def build_javdb_provider(
        username: str | None = None,
        password: str | None = None,
    ):
        """构建 JavDB provider；账号仅需登录的榜单（TOP250）需要，由插件从自身设置传入。"""
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider(username=username, password=password)

    @staticmethod
    def build_catalog_import_service():
        """构造目录导入服务。"""
        from src.service.catalog import CatalogImportService

        return CatalogImportService()

    def import_movie_by_number(
        self,
        movie_number: str,
        *,
        force_subscribed: bool = False,
    ) -> MovieSnapshot:
        """按 JavDB 优先规则导入，未找到时调用元数据来源插件；本地已存在则复用。

        返回不可变 MovieSnapshot（不暴露 ORM 对象）；插件要更新既有字段，必须
        重新取得 snapshot 并单独调用 ``context.movies.patch``。
        批量任务应分别构造并复用 provider/importer，避免每个番号重复创建客户端。
        """
        provider = self.build_javdb_provider()
        importer = self.build_catalog_import_service()
        from src.service.catalog.metadata_source_service import MetadataSourceService

        movie, _created = MetadataSourceService.import_by_number(
            movie_number, provider, importer,
            force_subscribed=force_subscribed,
        )
        return MovieApi._to_snapshot(movie)

    def list_existing_movie_numbers(self) -> set[str]:
        """主库全部影片番号的大写集合，供插件做 O(1) 存在性判定。"""
        from src.model import Movie

        return {
            (row[0] or "").upper()
            for row in Movie.select(Movie.movie_number).tuples()
        }

    def import_subtitle(
        self,
        movie_number: str,
        content: bytes,
        filename: str,
        language: str | None = None,
    ):
        """给影片写入一段字幕内容；统一处理扩展名校验、去重、落盘与登记。"""
        from src.service.catalog.subtitle_asset_service import SubtitleAssetService

        return SubtitleAssetService.import_subtitle_content(
            movie_number,
            content,
            filename,
            language=language,
        )

    def sync_ranking_sources(
        self,
        progress_callback=None,
    ) -> dict[str, int]:
        """同步当前插件声明的全部排行榜来源，返回统计 dict。"""
        from src.service.discovery.ranking_service import (
            RANKING_SOURCE_OWNERS,
            RankingSyncService,
        )

        source_keys = tuple(
            source_key
            for source_key, owner in RANKING_SOURCE_OWNERS.items()
            if owner == self.plugin_id
        )
        if not source_keys:
            raise RuntimeError(f"插件 {self.plugin_id} 未注册排行榜来源")
        return RankingSyncService().sync_all_rankings(
            progress_callback=progress_callback,
            source_keys=source_keys,
        )

    def sync_ranking_board(
        self,
        source_key: str,
        board_key: str,
        period: str | None = None,
    ) -> dict[str, int | str]:
        """同步单个榜单；source_key 必须是本插件声明的来源。"""
        from src.service.discovery.ranking_service import (
            RANKING_SOURCE_OWNERS,
            RankingSyncService,
        )

        if RANKING_SOURCE_OWNERS.get(source_key) != self.plugin_id:
            raise ValueError(
                f"排行榜来源 {source_key} 不属于插件 {self.plugin_id}"
            )
        return RankingSyncService().sync_board_period(
            source_key=source_key,
            board_key=board_key,
            period=period,
        )

    @staticmethod
    def get_task_logger(name: str):
        from src.scheduler.logging import get_task_logger

        return get_task_logger(name)
