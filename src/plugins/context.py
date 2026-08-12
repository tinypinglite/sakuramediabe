"""插件访问宿主能力的稳定门面。

具体 service/provider 在方法内懒导入，避免插件加载阶段反向依赖任务注册表。
插件只允许使用本类方法与 ``src.plugins.types`` 中的公开类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    @staticmethod
    def build_javdb_provider():
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider()

    @staticmethod
    def build_catalog_import_service(skip_dmm: bool = False):
        """构造目录导入服务；skip_dmm=True 跳过 DMM 简介抓取（批量场景提速）。"""
        from src.service.catalog import CatalogImportService

        return CatalogImportService(skip_dmm=skip_dmm)

    def import_movie_by_number(
        self,
        movie_number: str,
        *,
        force_subscribed: bool = False,
    ):
        """通过 JavDB 获取影片详情并复用核心目录入库能力。

        批量任务应分别构造并复用 provider/importer，避免每个番号重复创建客户端。
        """
        provider = self.build_javdb_provider()
        importer = self.build_catalog_import_service()
        detail = provider.get_movie_by_number(movie_number)
        return importer.upsert_movie_from_javdb_detail(
            detail,
            force_subscribed=force_subscribed,
        )

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
        task_run_id: int | None = None,
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
            task_run_id=task_run_id,
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
