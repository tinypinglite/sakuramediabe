"""按启用顺序调用元数据插件，并在调用期间校验交付文件。"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from PIL import Image as PillowImage

from src.common import normalize_movie_number
from src.common.service_helpers import find_movie_by_number
from src.config.config import settings
from src.i18n import translate
from src.i18n.text import resolve_text
from src.metadata.provider import MetadataNotFoundError
from src.plugins.extensions.metadata import (
    METADATA_SOURCE_EXTENSION_KEY,
    PluginMovieMetadata,
    validate_metadata_extension,
)


@dataclass(frozen=True)
class MetadataSourceFailure:
    plugin_id: str
    source_name: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.plugin_id,
            "source_name": self.source_name,
            "reason": "metadata_fetch_failed",
            "detail": self.detail,
        }


class MetadataSourceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failures: tuple[MetadataSourceFailure, ...] = (),
    ):
        self.failures = failures
        super().__init__(message)


@dataclass(frozen=True)
class MetadataFetchOutcome:
    detail: PluginMovieMetadata
    source: dict[str, str | None]
    source_errors: tuple[MetadataSourceFailure, ...] = ()


class MetadataSourceService:
    sources = ()

    @classmethod
    def import_by_number(
        cls,
        movie_number,
        import_service,
        *,
        force_subscribed=False,
    ):
        existing = find_movie_by_number(movie_number)
        if existing is not None:
            return existing, False
        with cls.fetch(movie_number) as outcome:
            return import_service.import_movie(
                outcome.detail,
                outcome.source,
                force_subscribed=force_subscribed,
            )

    @classmethod
    def register(cls, registrations):
        cls.sources = tuple(
            (
                registration.plugin_id,
                registration.display_name,
                registration.display_name_i18n,
                validate_metadata_extension(
                    plugin_id=registration.plugin_id, extension=extension
                ),
            )
            for registration in registrations
            for extension in registration.extensions
            if extension.key == METADATA_SOURCE_EXTENSION_KEY
        )

    @classmethod
    def enabled_plugin_sources(cls):
        """按宿主配置顺序返回当前启用且已注册的元数据插件。"""
        sources = {
            plugin_id: (name, name_i18n, source)
            for plugin_id, name, name_i18n, source in cls.sources
        }
        return tuple(
            (plugin_id, *sources[plugin_id])
            for plugin_id in settings.plugins.enabled
            if plugin_id in sources
        )

    @classmethod
    def is_plugin_enabled(cls, plugin_id: str) -> bool:
        return any(
            current_id == plugin_id
            for current_id, _, _, _ in cls.enabled_plugin_sources()
        )

    @staticmethod
    def _delivery_paths(detail: PluginMovieMetadata, root: Path) -> Iterator[Path]:
        request_dir = None
        for raw in [detail.cover_image_path, *detail.plot_image_paths]:
            path = Path(raw).resolve()
            relative = path.relative_to(root.resolve())
            if len(relative.parts) < 2 or not path.is_file():
                raise ValueError("图片必须是 metadata-tmp/<请求目录>/ 内的普通文件")
            current_dir = relative.parts[0]
            if request_dir is not None and current_dir != request_dir:
                raise ValueError("同一结果的图片必须来自同一请求目录")
            request_dir = current_dir
            yield path

    @staticmethod
    def _cleanup_delivery(paths: set[Path]):
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("元数据交付文件清理失败 path={} detail={}", path, exc)
        for directory in {path.parent for path in paths}:
            try:
                directory.rmdir()
            except OSError:
                pass

    @classmethod
    def _load_plugin(cls, plugin_id: str, movie_number: str):
        source_entry = next(
            (
                (name, name_i18n, source)
                for current_id, name, name_i18n, source in cls.enabled_plugin_sources()
                if current_id == plugin_id
            ),
            None,
        )
        if source_entry is None:
            raise MetadataSourceError(f"元数据插件未启用或未注册: {plugin_id}")
        name, name_i18n, source = source_entry
        display_name = resolve_text(name, name_i18n)
        paths: set[Path] = set()
        try:
            result = source.fetch_movie(movie_number)
            if result is None:
                raise MetadataNotFoundError("movie", movie_number)
            detail = PluginMovieMetadata.model_validate(result)
            root = (
                Path(settings.plugins.root_dir)
                / plugin_id
                / "data"
                / "metadata-tmp"
            )
            root.resolve().relative_to(
                (Path(settings.plugins.root_dir) / plugin_id).resolve()
            )
            paths.update(cls._delivery_paths(detail, root))
            if normalize_movie_number(detail.movie_number) != normalize_movie_number(
                movie_number
            ):
                raise ValueError("插件返回的番号与请求不匹配")
            for path in paths:
                with PillowImage.open(path) as image:
                    image.load()
        except MetadataNotFoundError:
            cls._cleanup_delivery(paths)
            raise
        except Exception as exc:
            cls._cleanup_delivery(paths)
            logger.warning(
                "元数据插件失败 plugin={} movie_number={} detail={}",
                plugin_id,
                movie_number,
                exc,
            )
            raise MetadataSourceError(
                f"{display_name}: {type(exc).__name__}: {exc}",
                failures=(
                    MetadataSourceFailure(
                        plugin_id=plugin_id,
                        source_name=display_name,
                        detail=str(exc),
                    ),
                ),
            ) from exc
        return detail, {
            "plugin_id": plugin_id,
            "display_name": display_name,
            "source_id": detail.source_id,
            "source_url": detail.source_url,
        }, paths

    @classmethod
    @contextmanager
    def fetch_plugin(cls, plugin_id: str, movie_number: str):
        """只调用指定插件，并在调用方消费完结果后清理插件交付文件。"""
        detail, source, paths = cls._load_plugin(plugin_id, movie_number)
        try:
            yield detail, source
        finally:
            cls._cleanup_delivery(paths)

    @classmethod
    @contextmanager
    def fetch(cls, movie_number: str):
        failures: list[MetadataSourceFailure] = []
        sources = cls.enabled_plugin_sources()
        if not sources:
            raise MetadataSourceError(translate("errors.metadata_source_unavailable"))
        for plugin_id, _name, _name_i18n, _source in sources:
            try:
                detail, source, paths = cls._load_plugin(plugin_id, movie_number)
            except MetadataNotFoundError:
                continue
            except MetadataSourceError as exc:
                failures.extend(exc.failures)
                continue
            try:
                yield MetadataFetchOutcome(
                    detail=detail,
                    source=source,
                    source_errors=tuple(failures),
                )
            finally:
                cls._cleanup_delivery(paths)
            return
        if failures:
            raise MetadataSourceError(
                "; ".join(item.detail for item in failures),
                failures=tuple(failures),
            )
        raise MetadataNotFoundError("movie", movie_number)
