"""插件访问宿主能力的稳定门面。

具体 service/provider 在方法内懒导入，避免插件加载阶段反向依赖任务注册表。
插件只允许使用本类方法与 ``src.plugins.types`` 中的公开类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.plugins.types import MOVIE_SNAPSHOT_FIELDS, MovieSnapshot


class MovieApi:
    """``context.movies``：影片只读快照与受保护字段 patch（v2-lite 契约 v2）。

    读取和 patch 都不暴露 ORM 对象；写入只经 MovieOwnershipGateway，插件拿不到
    任何可写句柄。字段 owner 与 revision 语义见 v2-lite 设计文档第 3/4 节。
    """

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @staticmethod
    def _to_snapshot(movie) -> MovieSnapshot:
        return MovieSnapshot(
            movie_id=movie.id,
            revision=movie.mutation_revision,
            values={
                name: getattr(movie, name) for name in MOVIE_SNAPSHOT_FIELDS
            },
            owners=dict(movie.field_owners or {}),
        )

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

        snapshots: list[MovieSnapshot] = []
        seen_ids: set[int] = set()
        for number in numbers:
            movie = find_movie_by_number(number)
            if movie is None or movie.id in seen_ids:
                continue
            seen_ids.add(movie.id)
            snapshots.append(self._to_snapshot(movie))
        return snapshots

    def patch(
        self,
        movie_id: int,
        fields: dict[str, Any],
        expected_revision: int,
    ) -> bool:
        """写受保护字段（白名单内）并取得/续持 owner，乐观并发提交。

        revision 不匹配或字段已被其他插件接管时返回 False 且整次零修改；
        插件应重新读取 snapshot 后决定是否重试。
        """
        from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

        return MovieOwnershipGateway.patch_plugin(
            movie_id,
            self._plugin_id,
            fields,
            expected_revision,
        )


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
    def movies(self) -> MovieApi:
        """影片只读快照与受保护字段写入出口（v2-lite 契约 v2）。"""
        return MovieApi(self.plugin_id)

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
        """通过 JavDB 获取影片详情并复用核心目录入库能力；已存在影片跳过不更新。

        返回不可变 MovieSnapshot（不暴露 ORM 对象）；插件要更新既有字段，必须
        重新取得 snapshot 并单独调用 ``context.movies.patch``。
        批量任务应分别构造并复用 provider/importer，避免每个番号重复创建客户端。
        """
        provider = self.build_javdb_provider()
        importer = self.build_catalog_import_service()
        detail = provider.get_movie_by_number(movie_number)
        movie, _created = importer.import_movie_if_missing(
            detail,
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
