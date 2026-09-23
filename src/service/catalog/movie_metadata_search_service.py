"""只搜索影片元数据，不创建 Movie / Image 目录记录。"""

from __future__ import annotations

import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger
from PIL import Image as PillowImage

from src.api.exception.errors import ApiError
from src.common import (
    build_signed_image_url,
    normalize_movie_number,
)
from src.common.media_paths import media_image_root_path
from src.metadata._providers.models import JavdbMovieDetailResource
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError
from src.plugins.extensions.metadata import PluginMovieMetadata
from src.schema.transfers.media_import import (
    ImportMetadataCandidateResource,
    ImportMetadataSearchResponse,
    ImportMetadataSourceErrorResource,
)
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.catalog.movie_image_service import MovieImageService


class MovieMetadataSearchService:
    """提供人工重试所需的短期元数据候选和可展示封面。"""

    SEARCH_ASSET_DIR = "metadata-search"
    SEARCH_ASSET_MAX_AGE_SECONDS = 24 * 60 * 60
    _IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

    @classmethod
    def search_by_number(cls, movie_number: str) -> ImportMetadataSearchResponse:
        normalized_number = normalize_movie_number(movie_number)
        if not normalized_number:
            raise ApiError(422, "invalid_movie_number", "番号不能为空")

        search_id = uuid4().hex
        search_root = media_image_root_path() / cls.SEARCH_ASSET_DIR / search_id
        image_service = MovieImageService()
        candidates: list[ImportMetadataCandidateResource] = []
        source_errors: list[ImportMetadataSourceErrorResource] = []
        detail = None
        try:
            provider = build_javdb_provider()
            try:
                detail = provider.get_movie_by_number(normalized_number)
            except MetadataNotFoundError:
                detail = None
            except Exception as exc:
                source_errors.append(
                    ImportMetadataSourceErrorResource(
                        source="javdb",
                        source_name="JavDB",
                        reason=type(exc).__name__,
                        detail=str(exc),
                    )
                )
            # JavDB 命中即番号严格相等，视为权威来源，不再查询插件。
            if detail is not None:
                candidates.append(
                    cls._javdb_candidate(
                        detail,
                        normalized_number,
                        image_service,
                        search_root,
                        len(candidates),
                    )
                )
            else:
                for plugin_id, plugin_name, _source in MetadataSourceService.enabled_plugin_sources():
                    try:
                        with MetadataSourceService.fetch_plugin(
                            plugin_id, normalized_number
                        ) as (plugin_detail, source):
                            candidates.append(
                                cls._plugin_candidate(
                                    plugin_id,
                                    plugin_detail,
                                    source,
                                    normalized_number,
                                    search_root,
                                    len(candidates),
                                )
                            )
                    except MetadataNotFoundError:
                        continue
                    except Exception as exc:
                        source_errors.append(
                            ImportMetadataSourceErrorResource(
                                source=plugin_id,
                                source_name=plugin_name,
                                reason=type(exc).__name__,
                                detail=str(exc),
                            )
                        )
                        logger.warning(
                            "Manual metadata search plugin failed plugin={} movie_number={} detail={}",
                            plugin_id,
                            normalized_number,
                            exc,
                        )
        finally:
            image_service.http_client.close()

        if not candidates:
            shutil.rmtree(search_root, ignore_errors=True)
        return ImportMetadataSearchResponse(
            movie_number=normalized_number,
            candidates=candidates,
            source_errors=source_errors,
        )

    @classmethod
    def _javdb_candidate(
        cls,
        detail: JavdbMovieDetailResource,
        normalized_number: str,
        image_service: MovieImageService,
        search_root: Path,
        index: int,
    ) -> ImportMetadataCandidateResource:
        return ImportMetadataCandidateResource(
            candidate_id=cls._javdb_candidate_id(normalized_number, detail.javdb_id),
            source="javdb",
            source_name="JavDB",
            javdb_id=detail.javdb_id,
            movie_number=detail.movie_number,
            title=detail.title,
            cover_url=cls._cache_remote_cover(
                image_service,
                detail.cover_image,
                search_root,
                index,
            ),
            release_date=detail.release_date,
            duration_minutes=detail.duration_minutes,
        )

    @classmethod
    def _plugin_candidate(
        cls,
        plugin_id: str,
        detail: PluginMovieMetadata,
        source: dict[str, str | None],
        normalized_number: str,
        search_root: Path,
        index: int,
    ) -> ImportMetadataCandidateResource:
        return ImportMetadataCandidateResource(
            candidate_id=cls._plugin_candidate_id(plugin_id, normalized_number),
            source="plugin",
            source_name=str(source.get("display_name") or plugin_id),
            source_id=detail.source_id,
            movie_number=detail.movie_number,
            title=detail.title,
            cover_url=cls._cache_local_cover(
                detail.cover_image_path,
                search_root,
                index,
            ),
            release_date=detail.release_date.isoformat(),
            duration_minutes=detail.duration_minutes,
        )

    @classmethod
    def _cache_remote_cover(
        cls,
        image_service: MovieImageService,
        image_url: str | None,
        search_root: Path,
        index: int,
    ) -> str | None:
        if not image_url:
            return None
        extension = cls._image_extension(urlparse(image_url).path)
        target = search_root / f"{index}{extension}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image_service.image_downloader(image_url, target)
            cls._validate_image(target)
            return cls._signed_asset_url(target)
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.warning("Manual metadata cover cache failed url={} detail={}", image_url, exc)
            return None

    @classmethod
    def _cache_local_cover(
        cls,
        image_path: str,
        search_root: Path,
        index: int,
    ) -> str | None:
        extension = cls._image_extension(Path(image_path).suffix)
        target = search_root / f"{index}{extension}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image_path, target)
            cls._validate_image(target)
            return cls._signed_asset_url(target)
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.warning("Manual metadata plugin cover cache failed path={} detail={}", image_path, exc)
            return None

    @classmethod
    def _signed_asset_url(cls, path: Path) -> str:
        relative_path = path.relative_to(media_image_root_path()).as_posix()
        return build_signed_image_url(relative_path)

    @classmethod
    def _validate_image(cls, path: Path) -> None:
        with PillowImage.open(path) as image:
            image.load()

    @classmethod
    def _image_extension(cls, raw_path: str) -> str:
        extension = Path(raw_path or "").suffix.lower()
        return extension if extension in cls._IMAGE_EXTENSIONS else ".jpg"

    @staticmethod
    def _javdb_candidate_id(movie_number: str, javdb_id: str) -> str:
        return f"javdb:{movie_number}:{javdb_id}"

    @staticmethod
    def _plugin_candidate_id(plugin_id: str, movie_number: str) -> str:
        return f"plugin:{plugin_id}:{movie_number}"

    @classmethod
    def resolve_candidate_reference(cls, candidate_id: str) -> dict[str, str]:
        parts = (candidate_id or "").strip().split(":")
        if len(parts) == 3 and parts[0] == "javdb":
            _, movie_number, javdb_id = parts
            if movie_number and javdb_id:
                return {
                    "source": "javdb",
                    "movie_number": normalize_movie_number(movie_number),
                    "javdb_id": javdb_id,
                }
        if len(parts) == 3 and parts[0] == "plugin":
            _, plugin_id, movie_number = parts
            if (
                plugin_id
                and movie_number
                and MetadataSourceService.is_plugin_enabled(plugin_id)
            ):
                return {
                    "source": "plugin",
                    "plugin_id": plugin_id,
                    "movie_number": normalize_movie_number(movie_number),
                }
        raise ApiError(422, "invalid_metadata_candidate", "元数据候选无效或已失效")

    @classmethod
    @contextmanager
    def fetch_candidate(cls, candidate_id: str):
        reference = cls.resolve_candidate_reference(candidate_id)
        provider = build_javdb_provider()
        if reference["source"] == "javdb":
            detail = provider.get_movie_by_javdb_id(reference["javdb_id"])
            cls._ensure_candidate_number(detail.movie_number, reference["movie_number"])
            yield detail, "javdb", provider, None
            return

        with MetadataSourceService.fetch_plugin(
            reference["plugin_id"], reference["movie_number"]
        ) as (detail, source):
            yield detail, "plugin", provider, source

    @staticmethod
    def _ensure_candidate_number(actual: str, expected: str) -> None:
        if normalize_movie_number(actual) != normalize_movie_number(expected):
            raise ApiError(422, "metadata_candidate_mismatch", "元数据候选番号不匹配")

    @classmethod
    def cleanup_search_assets(cls) -> int:
        root = media_image_root_path() / cls.SEARCH_ASSET_DIR
        if not root.is_dir() or root.is_symlink():
            return 0
        now = time.time()
        deleted = 0
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            try:
                stale = now - child.stat().st_mtime > cls.SEARCH_ASSET_MAX_AGE_SECONDS
            except OSError:
                continue
            if not stale:
                continue
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
        try:
            root.rmdir()
        except OSError:
            pass
        return deleted
