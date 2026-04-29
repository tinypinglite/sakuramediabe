import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests
from loguru import logger
from PIL import Image as PillowImage

from src.common import build_signed_image_url, normalize_movie_number
from src.config.config import settings
from sakuramedia_metadata_providers.exceptions import (
    MissavThumbnailNotFoundError,
    MissavThumbnailRequestError,
)
from sakuramedia_metadata_providers.models import MissavThumbnailManifest
from sakuramedia_metadata_providers.providers.missav import MissavThumbnailProvider
from src.schema.catalog.movies import (
    MissavThumbnailItemResource,
    MissavThumbnailResource,
)


@dataclass(slots=True)
class MissavThumbnailCachePaths:
    base_dir: Path
    sprite_dir: Path
    frame_dir: Path
    metadata_path: Path


class MissavThumbnailService:
    DOWNLOAD_TIMEOUT_SECONDS = 30
    DOWNLOAD_IMPERSONATE = "chrome124"
    SPRITE_DOWNLOAD_MAX_WORKERS = 4
    SLICE_PROGRESS_STEP = 25

    def __init__(
        self,
        provider: MissavThumbnailProvider | None = None,
        sprite_downloader=None,
    ):
        self.provider = provider or self._build_provider()
        self.sprite_downloader = sprite_downloader or self._download_sprite

    @staticmethod
    def _build_provider() -> MissavThumbnailProvider:
        from src.metadata.factory import build_missav_thumbnail_provider

        return build_missav_thumbnail_provider()

    def get_movie_thumbnails(
        self,
        movie_number: str,
        *,
        refresh: bool = False,
        progress_callback: Callable[[str, dict], None] | None = None,
    ) -> MissavThumbnailResource:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            raise MissavThumbnailNotFoundError(movie_number, "movie number is invalid")

        cache_paths = self._build_cache_paths(normalized_movie_number)
        if refresh:
            logger.debug("Missav thumbnail refresh requested movie_number={}", normalized_movie_number)
            self._clear_cache(cache_paths.base_dir)

        cached_manifest = self._load_cached_manifest(cache_paths.metadata_path)
        if cached_manifest is not None and self._frames_are_complete(cached_manifest, cache_paths.frame_dir):
            logger.debug(
                "Missav thumbnail cache hit movie_number={} total={}",
                normalized_movie_number,
                cached_manifest.pic_num,
            )
            return self._build_resource(cached_manifest, cache_paths.frame_dir)

        manifest = self.provider.fetch_thumbnail_manifest(normalized_movie_number)
        self._emit_progress(
            progress_callback,
            "manifest_resolved",
            {
                "movie_number": manifest.movie_number,
                "sprite_total": len(manifest.urls),
                "thumbnail_total": manifest.pic_num,
            },
        )
        self._rebuild_cache(
            manifest,
            cache_paths,
            progress_callback=progress_callback,
        )
        return self._build_resource(manifest, cache_paths.frame_dir)

    def _rebuild_cache(
        self,
        manifest: MissavThumbnailManifest,
        cache_paths: MissavThumbnailCachePaths,
        *,
        progress_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._clear_cache(cache_paths.base_dir)
        cache_paths.sprite_dir.mkdir(parents=True, exist_ok=True)
        cache_paths.frame_dir.mkdir(parents=True, exist_ok=True)

        sprite_paths = self._download_sprites(
            manifest,
            cache_paths.sprite_dir,
            progress_callback=progress_callback,
        )
        self._slice_sprites(
            manifest=manifest,
            sprite_paths=sprite_paths,
            frame_dir=cache_paths.frame_dir,
            progress_callback=progress_callback,
        )
        cache_paths.metadata_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _build_cache_paths(movie_number: str) -> MissavThumbnailCachePaths:
        base_dir = Path(settings.media.import_image_root_path).expanduser()
        if not base_dir.is_absolute():
            base_dir = (Path.cwd() / base_dir).resolve()

        movie_root = base_dir / "movies" / movie_number / "missav-seek"
        return MissavThumbnailCachePaths(
            base_dir=movie_root,
            sprite_dir=movie_root / "sprites",
            frame_dir=movie_root / "frames",
            metadata_path=movie_root / "metadata.json",
        )

    @staticmethod
    def _clear_cache(base_dir: Path) -> None:
        if base_dir.exists():
            shutil.rmtree(base_dir)

    def _load_cached_manifest(self, metadata_path: Path) -> MissavThumbnailManifest | None:
        if not metadata_path.exists() or not metadata_path.is_file():
            return None

        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            return MissavThumbnailManifest.from_dict(payload)
        except Exception as exc:
            logger.warning(
                "Missav thumbnail cache metadata invalid path={} detail={}",
                str(metadata_path),
                exc,
            )
            return None

    @staticmethod
    def _frames_are_complete(manifest: MissavThumbnailManifest, frame_dir: Path) -> bool:
        if not frame_dir.exists() or not frame_dir.is_dir():
            return False

        for frame_index in range(manifest.pic_num):
            if not (frame_dir / f"{frame_index}.jpg").exists():
                return False
        return True

    def _download_sprites(
        self,
        manifest: MissavThumbnailManifest,
        sprite_dir: Path,
        *,
        progress_callback: Callable[[str, dict], None] | None = None,
    ) -> list[Path]:
        sprite_paths = [sprite_dir / f"{index}.jpg" for index in range(len(manifest.urls))]
        if not manifest.urls:
            raise MissavThumbnailNotFoundError(manifest.movie_number, "sprite urls missing")

        max_workers = min(self.SPRITE_DOWNLOAD_MAX_WORKERS, len(manifest.urls))
        completed_count = 0
        self._emit_progress(
            progress_callback,
            "download_started",
            {"total": len(manifest.urls)},
        )
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="missav-seek") as executor:
            future_map = {
                executor.submit(
                    self.sprite_downloader,
                    sprite_url,
                    sprite_path,
                    manifest.page_url,
                ): (sprite_url, sprite_path)
                for sprite_url, sprite_path in zip(manifest.urls, sprite_paths)
            }
            for future in as_completed(future_map):
                sprite_url, sprite_path = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    raise MissavThumbnailRequestError(
                        sprite_url,
                        f"sprite_download_failed:{sprite_path.name}:{exc}",
                    ) from exc
                completed_count += 1
                self._emit_progress(
                    progress_callback,
                    "download_progress",
                    {
                        "completed": completed_count,
                        "total": len(manifest.urls),
                    },
                )
        self._emit_progress(
            progress_callback,
            "download_finished",
            {
                "completed": len(manifest.urls),
                "total": len(manifest.urls),
            },
        )
        return sprite_paths

    def _download_sprite(self, sprite_url: str, target_path: Path, page_url: str) -> None:
        try:
            response = requests.get(
                sprite_url,
                headers={
                    "referer": page_url,
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                impersonate=self.DOWNLOAD_IMPERSONATE,
                timeout=self.DOWNLOAD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise MissavThumbnailRequestError(sprite_url, str(exc)) from exc

        if response.status_code != 200:
            raise MissavThumbnailRequestError(
                sprite_url,
                f"unexpected_status_code:{response.status_code}",
            )
        target_path.write_bytes(response.content)

    def _slice_sprites(
        self,
        *,
        manifest: MissavThumbnailManifest,
        sprite_paths: list[Path],
        frame_dir: Path,
        progress_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        frames_per_sprite = manifest.col * manifest.row
        if frames_per_sprite <= 0:
            raise MissavThumbnailRequestError(manifest.page_url, "invalid frames_per_sprite")

        sliced_count = 0
        self._emit_progress(
            progress_callback,
            "slice_started",
            {"total": manifest.pic_num},
        )
        for sprite_index, sprite_path in enumerate(sprite_paths):
            start_index = sprite_index * frames_per_sprite
            if start_index >= manifest.pic_num:
                break

            end_index = min(start_index + frames_per_sprite, manifest.pic_num)
            try:
                with PillowImage.open(sprite_path) as sprite_image:
                    for frame_index in range(start_index, end_index):
                        local_index = frame_index - start_index
                        crop_box = self._build_crop_box(manifest, local_index)
                        if crop_box[2] > sprite_image.width or crop_box[3] > sprite_image.height:
                            raise MissavThumbnailRequestError(
                                manifest.page_url,
                                f"sprite_crop_out_of_bounds:{sprite_path.name}:{crop_box}",
                            )

                        frame_image = sprite_image.crop(crop_box)
                        if frame_image.mode not in ("RGB", "L"):
                            frame_image = frame_image.convert("RGB")
                        elif frame_image.mode == "L":
                            frame_image = frame_image.convert("RGB")
                        frame_image.save(frame_dir / f"{frame_index}.jpg", format="JPEG", quality=90)
                        sliced_count += 1
                        if (
                            sliced_count % self.SLICE_PROGRESS_STEP == 0
                            or sliced_count == manifest.pic_num
                        ):
                            self._emit_progress(
                                progress_callback,
                                "slice_progress",
                                {
                                    "completed": sliced_count,
                                    "total": manifest.pic_num,
                                },
                            )
            except MissavThumbnailRequestError:
                raise
            except Exception as exc:
                raise MissavThumbnailRequestError(
                    manifest.page_url,
                    f"sprite_slice_failed:{sprite_path.name}:{exc}",
                ) from exc
        self._emit_progress(
            progress_callback,
            "slice_finished",
            {
                "completed": manifest.pic_num,
                "total": manifest.pic_num,
            },
        )

    @staticmethod
    def _build_crop_box(manifest: MissavThumbnailManifest, local_index: int) -> tuple[int, int, int, int]:
        # missav 播放器里 row 才是横向格子数，col 是纵向格子数，这里保持同样的切图顺序。
        x_index = local_index % manifest.row
        y_index = local_index // manifest.row
        left = manifest.offset_x + x_index * manifest.width
        upper = manifest.offset_y + y_index * manifest.height
        right = left + manifest.width
        lower = upper + manifest.height
        return left, upper, right, lower

    def _build_resource(
        self,
        manifest: MissavThumbnailManifest,
        frame_dir: Path,
    ) -> MissavThumbnailResource:
        items: list[MissavThumbnailItemResource] = []
        for frame_index in range(manifest.pic_num):
            frame_path = frame_dir / f"{frame_index}.jpg"
            relative_path = frame_path.relative_to(self._image_root_path()).as_posix()
            items.append(
                MissavThumbnailItemResource(
                    index=frame_index,
                    url=build_signed_image_url(relative_path),
                )
            )
        return MissavThumbnailResource(
            movie_number=manifest.movie_number,
            source="missav",
            total=len(items),
            items=items,
        )

    @staticmethod
    def _image_root_path() -> Path:
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    @staticmethod
    def _emit_progress(
        progress_callback: Callable[[str, dict], None] | None,
        event: str,
        payload: dict,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(event, payload)
