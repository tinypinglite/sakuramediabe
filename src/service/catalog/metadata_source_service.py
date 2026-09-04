"""JavDB 优先；插件结果只在本次调用期间交付给宿主。"""

from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from peewee import fn
from PIL import Image as PillowImage

from src.common import normalize_movie_number
from src.common.service_helpers import find_movie_by_number
from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor
from src.plugins.extensions.metadata import (
    METADATA_SOURCE_EXTENSION_KEY,
    PluginMovieMetadata,
    validate_metadata_extension,
)


class MetadataSourceError(RuntimeError):
    pass


class MetadataSourceService:
    sources = ()

    @classmethod
    def import_by_number(
        cls, movie_number, provider, import_service, *, force_subscribed=False
    ):
        existing = find_movie_by_number(movie_number)
        if existing is not None:
            return existing, False
        with cls.fetch(movie_number, provider) as (detail, source):
            if source is not None:
                return import_service.import_plugin_movie(
                    detail, source, provider, force_subscribed=force_subscribed
                )
            return import_service.import_movie_if_missing(
                detail, force_subscribed=force_subscribed
            )

    @classmethod
    def register(cls, registrations):
        cls.sources = tuple(
            (
                registration.plugin_id,
                registration.display_name,
                validate_metadata_extension(
                    plugin_id=registration.plugin_id, extension=extension
                ),
            )
            for registration in registrations
            for extension in registration.extensions
            if extension.key == METADATA_SOURCE_EXTENSION_KEY
        )

    @staticmethod
    def _delivery_paths(detail: PluginMovieMetadata, root: Path) -> list[Path]:
        paths = []
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
            paths.append(path)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _cleanup_delivery(paths: list[Path]):
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
    @contextmanager
    def fetch(cls, movie_number: str, provider):
        try:
            detail = provider.get_movie_by_number(movie_number)
        except MetadataNotFoundError:
            pass
        else:
            yield detail, None
            return

        failures = []
        # enabled 是宿主有序配置，同时确保已停用的插件不再被调用。
        sources = {plugin_id: (name, source) for plugin_id, name, source in cls.sources}
        for plugin_id in settings.plugins.enabled:
            if plugin_id not in sources:
                continue
            name, source = sources[plugin_id]
            paths = []
            try:
                result = source.fetch_movie(movie_number)
                if result is None:
                    continue
                detail = PluginMovieMetadata.model_validate(result)
                root = (
                    Path(settings.plugins.root_dir)
                    / plugin_id
                    / "data"
                    / "metadata-tmp"
                )
                root.resolve().relative_to((Path(settings.plugins.root_dir) / plugin_id).resolve())
                paths = cls._delivery_paths(detail, root)
                if normalize_movie_number(
                    detail.movie_number
                ) != normalize_movie_number(movie_number):
                    raise ValueError("插件返回的番号与请求不匹配")
                for path in paths:
                    with PillowImage.open(path) as image:
                        image.load()
            except Exception as exc:
                cls._cleanup_delivery(paths)
                failures.append(
                    f"{plugin_id}: 元数据查询或交付校验失败 ({type(exc).__name__})"
                )
                logger.warning("元数据插件失败 plugin={} detail={}", plugin_id, exc)
                continue
            try:
                yield (
                    detail,
                    {
                        "plugin_id": plugin_id,
                        "display_name": name,
                        "source_id": detail.source_id,
                        "source_url": detail.source_url,
                    },
                )
            finally:
                cls._cleanup_delivery(paths)
            return
        if failures:
            raise MetadataSourceError("; ".join(failures))
        raise MetadataNotFoundError("movie", movie_number)

    @staticmethod
    def match_actors(actors, provider, import_service) -> list[Actor]:
        matched = {}
        for actor in actors:
            names = {
                value.strip().casefold()
                for value in [actor.name, *actor.alias_names]
                if value.strip()
            }
            condition = fn.LOWER(Actor.name).in_(names)
            for name in names:
                condition |= fn.LOWER(Actor.alias_name).contains(name)
            candidates = {
                item.javdb_id: item
                for item in Actor.select().where(condition)
                if names
                & {
                    value.strip().casefold()
                    for value in [item.name, *item.alias_name.split("/")]
                }
            }
            if len(candidates) == 1:
                matched.update(candidates)
                continue
            if len(candidates) > 1:
                continue
            try:
                candidates = {
                    item.javdb_id: item
                    for item in provider.search_actors(actor.name)
                    if item.javdb_id
                    and names
                    & {
                        value.strip().casefold()
                        for value in [item.name, *item.alias_names]
                    }
                }
                if len(candidates) == 1:
                    javdb_id, candidate = next(iter(candidates.items()))
                    matched[javdb_id] = import_service.upsert_actor_from_javdb_resource(
                        candidate
                    )
            except Exception as exc:
                logger.warning(
                    "元数据演员匹配失败，已过滤 name={} detail={}", actor.name, exc
                )
        return list(matched.values())
