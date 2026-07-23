from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PluginContext:
    """插件访问宿主能力的稳定门面。

    具体 service/provider 在方法内懒导入，避免插件加载阶段反向依赖任务注册表。
    """

    plugin_id: str
    settings: Mapping[str, Any]

    @staticmethod
    def build_javdb_provider():
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider()

    @staticmethod
    def build_catalog_import_service():
        from src.service.catalog import CatalogImportService

        return CatalogImportService()

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

    @staticmethod
    def get_task_logger(name: str):
        from src.scheduler.logging import get_task_logger

        return get_task_logger(name)
