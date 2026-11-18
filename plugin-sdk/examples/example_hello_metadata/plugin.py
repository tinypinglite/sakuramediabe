"""按番号返回静态影片元数据的示例插件。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from src.plugins import (
    HOST_API_VERSION,
    METADATA_SOURCE_EXTENSION_KEY,
    PluginContext,
    PluginExtension,
    PluginMetadataActor,
    PluginMetadataSource,
    PluginMovieMetadata,
    PluginRegistration,
)

from .settings import Settings
from .translations import text, text_map

_PLUGIN_ROOT = Path(__file__).resolve().parent
_MANIFEST = json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = _MANIFEST["plugin_id"]
VERSION = _MANIFEST["version"]
DISPLAY_NAME = text("manifest.display_name", "zh-CN", _MANIFEST["display_name"])
TEST_MOVIE_NUMBER = "EXAMPLE-001"
_COVER_ASSET = _PLUGIN_ROOT / "assets" / "cover.png"


def _deliver_cover(context: PluginContext) -> str:
    """把封面复制到宿主约定的 data/metadata-tmp 交付目录。"""
    request_dir = context.data_dir / "metadata-tmp" / uuid4().hex
    request_dir.mkdir(parents=True, exist_ok=True)
    target = request_dir / "cover.png"
    target.write_bytes(_COVER_ASSET.read_bytes())
    return str(target)


def _fetch_movie(
    context: PluginContext,
    settings: Settings,
    movie_number: str,
) -> PluginMovieMetadata | None:
    if movie_number.strip().upper() != TEST_MOVIE_NUMBER:
        return None
    title = context.i18n.t("metadata.title", default="示例影片")
    return PluginMovieMetadata(
        movie_number=TEST_MOVIE_NUMBER,
        title=f"{settings.title_prefix}{title}",
        release_date=date(2026, 1, 1),
        duration_minutes=120,
        cover_image_path=_deliver_cover(context),
        summary=context.i18n.t(
            "metadata.summary",
            default="这是 SakuraMedia 插件开发示例返回的静态简介。",
        ),
        maker_name="示例片商",
        director_name="示例导演",
        series_name="示例系列",
        actors=[PluginMetadataActor(name="示例演员", alias_names=["Example Actor"])],
        tags=["示例", "静态数据"],
        source_url="https://example.com/movies/EXAMPLE-001",
        source_id="EXAMPLE-001",
    )


def register(context: PluginContext) -> PluginRegistration:
    settings = Settings.model_validate(context.settings)

    def fetch_movie(movie_number: str) -> PluginMovieMetadata | None:
        return _fetch_movie(context, settings, movie_number)

    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        display_name_i18n=text_map("manifest.display_name"),
        version=VERSION,
        host_api_version=HOST_API_VERSION,
        test_movie_number=TEST_MOVIE_NUMBER,
        extensions=(
            PluginExtension(
                key=METADATA_SOURCE_EXTENSION_KEY,
                data=PluginMetadataSource(fetch_movie=fetch_movie),
            ),
        ),
    )
