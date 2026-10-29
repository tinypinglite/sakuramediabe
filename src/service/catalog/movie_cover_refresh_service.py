"""把存量封面刷成 JavDB app CDN 的无水印原图（只换封面，手动任务）。

背景：历史实现的 ``JavdbProvider._normalize_image_url`` 把 app CDN 地址改写成
网页 CDN ``c0.jdbstatic.com``，而网页 CDN 的 ``covers/`` 带 ``javdb.com`` 半透明
水印——同一张图的另一份内容。取图链路修好后，本任务负责把存量封面刷干净。

范围只到封面：剧照与演员头像在两条 CDN 上同源，不需要重下。

优先级：已订阅影片优先（后订阅的先跑），其余按热度倒序。处理进度落在图片根同级的
``movie-cover-refresh/`` 账本里，断点续跑不重复下载；账本不是事实来源，删掉重跑
最多是重复下载，不会漏刷。

封面地址由 ``javdb_id`` 直接推导（省掉每部一次详情请求）：分片前缀是 javdb_id 的
小写前两位，租户段从 provider 的任意一次详情响应里取一次，404 时自动重新解析。
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from loguru import logger
from peewee import Query
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from src.common.media_paths import (
    media_image_root_path,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.i18n import translate
from src.metadata.factory import build_javdb_provider
from src.model import Image, Movie, MoviePlotImage, get_database
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.catalog.movie_image_write_service import apply_thin_cover_resolution
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

APP_CDN_HOST = "tp.spfcas.com"
COVER_DIR_NAME = "covers"
LEDGER_DIR_NAME = "movie-cover-refresh"
# 与 sakuramedia_asset_compact 的中档质量对齐，避免同一张图两套编码参数。
WEBP_QUALITY = 80
JPEG_QUALITY = 95
# 后缀 -> Pillow 保存格式；只放宿主图片入口确实能解码的格式。
PILLOW_FORMATS: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".gif": "GIF",
}
# Pillow 识别出的格式 -> 与之匹配的后缀；命中时直接复用下载字节，不做二次编码。
FORMAT_SUFFIXES: dict[str, frozenset[str]] = {
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "MPO": frozenset({".jpg", ".jpeg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
    "GIF": frozenset({".gif"}),
}


class MovieCoverRefreshParams(BaseModel):
    """手动任务参数：留空即按优先级把待刷新影片跑完。"""

    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(
        default=None,
        ge=1,
        title="本轮处理上限（部）",
        description="只统计真正处理的影片（跳过与失败不计入）；留空表示跑到队列末尾。",
    )
    movie_numbers: list[str] = Field(
        default_factory=list,
        title="指定番号",
        description="只刷新这些番号；留空按订阅/热度优先级顺序处理。",
    )
    dry_run: bool = Field(
        default=False,
        title="只校验不落库",
        description="照常下载与解码校验，但不替换封面、薄封面与资产包。",
    )
    concurrency: int = Field(
        default=6,
        ge=1,
        le=32,
        title="并发影片数",
        description="按批并发处理；下载/切割/打包都是 IO 或释放 GIL 的调用，并发能成倍缩短总时长。",
    )


class MovieCoverRefreshLedger:
    """处理进度账本：只记"哪些刷过、哪些失败"，事实来源始终是图片库本身。"""

    FLUSH_INTERVAL = 100

    def __init__(self, root: Path | None = None):
        self.root = root or (media_image_root_path().parent / LEDGER_DIR_NAME)
        self.done_path = self.root / "done.txt"
        self.failed_path = self.root / "failed.txt"
        self._pending_writes = 0
        self._lock = threading.Lock()

    def load_done(self) -> set[int]:
        if not self.done_path.is_file():
            return set()
        done: set[int] = set()
        for line in self.done_path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value.isdigit():
                done.add(int(value))
        return done

    def mark_done(self, movie_id: int) -> None:
        self._append(self.done_path, str(movie_id))

    def mark_failed(self, movie_id: int, reason: str) -> None:
        self._append(self.failed_path, f"{movie_id}\t{reason}")

    def _append(self, path: Path, line: str) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
                handle.flush()
                # 逐条 fsync 会把 16w 次写入拖成分钟级，按批落盘；崩溃最多重下几十张。
                self._pending_writes += 1
                if self._pending_writes >= self.FLUSH_INTERVAL:
                    os.fsync(handle.fileno())
                    self._pending_writes = 0


class MovieCoverRefreshService:
    """只替换封面的存量刷新服务；薄封面随封面重算，剧照/头像不动。"""

    TASK_KEY = "movie_cover_refresh"
    REQUEST_INTERVAL_SECONDS = 0.15
    TENANT_PATTERN = re.compile(r"^https?://[^/]+/(?P<tenant>[^/]+)/covers/")

    def __init__(self, provider=None, image_service=None, ledger=None):
        self.provider = provider or build_javdb_provider()
        self.image_service = image_service or MovieImageService()
        self.ledger = ledger or MovieCoverRefreshLedger()
        self._tenant: str | None = None
        self._tenant_lock = threading.Lock()

    # ------------------------------------------------------------------ 目标集合

    @staticmethod
    def targets(*, movie_numbers: list[str] | None = None) -> Query:
        """待刷新影片查询：已订阅优先（订阅时间倒序），其余按热度倒序。"""
        query = Movie.select(Movie.id, Movie.movie_number, Movie.javdb_id).where(
            Movie.javdb_id.is_null(False) & Movie.cover_image.is_null(False)
        )
        if movie_numbers:
            query = query.where(Movie.movie_number.in_(list(movie_numbers)))
        return query.order_by(
            Movie.is_subscribed.desc(),
            Movie.subscribed_at.desc(nulls="last"),
            Movie.heat.desc(),
            Movie.id.asc(),
        )

    @staticmethod
    def _cover_origin(movie_id: int) -> str | None:
        """当前封面 origin；决定新封面落盘后缀，避免 origin 抖动与格式回退。"""
        return (
            Image.select(Image.origin)
            .join(Movie, on=(Movie.cover_image == Image.id))
            .where(Movie.id == movie_id)
            .scalar()
        )

    @staticmethod
    def app_cover_url(javdb_id: str, tenant: str) -> str:
        """app CDN 封面地址：分片前缀取 javdb_id 的小写前两位。"""
        return (
            f"https://{APP_CDN_HOST}/{tenant}/{COVER_DIR_NAME}/"
            f"{javdb_id[:2].lower()}/{javdb_id}.jpg"
        )

    # ------------------------------------------------------------------ 主流程

    def run(self, *, reporter, params: MovieCoverRefreshParams | None = None) -> dict:
        options = params or MovieCoverRefreshParams()
        done_ids = self.ledger.load_done()
        query = self.targets(movie_numbers=options.movie_numbers)
        total = query.count()
        stats: dict[str, int] = {
            "target_movies": total,
            "processed_movies": 0,
            "refreshed_movies": 0,
            "failed_movies": 0,
            "skipped_movies": 0,
        }
        stats_lock = threading.Lock()
        emit_lock = threading.Lock()
        logger.info(
            "Movie cover refresh started target_movies={} already_done={} limit={} concurrency={} dry_run={}",
            total,
            len(done_ids),
            options.limit,
            options.concurrency,
            options.dry_run,
        )

        def progress_text(completed: int, *, movie_number: str | None = None) -> str:
            params = {
                "current": completed,
                "total": total,
                "refreshed": stats["refreshed_movies"],
                "failed": stats["failed_movies"],
                "skipped": stats["skipped_movies"],
            }
            if movie_number:
                return translate(
                    "tasks.movie_cover_refresh.progress.refreshing",
                    movie_number=movie_number,
                    **params,
                )
            return translate("tasks.movie_cover_refresh.progress.running", **params)

        def emit(completed: int, *, movie_number: str | None = None) -> None:
            with emit_lock:
                reporter.emit(
                    current=completed,
                    total=total,
                    text=progress_text(completed, movie_number=movie_number),
                    summary_patch=dict(stats),
                )
            if completed % 200 == 0:
                logger.info(
                    "Movie cover refresh progress completed={}/{} refreshed={} failed={} skipped={}",
                    completed,
                    total,
                    stats["refreshed_movies"],
                    stats["failed_movies"],
                    stats["skipped_movies"],
                )

        def handle(movie: Movie) -> None:
            """单部刷新 + 账本记录；异常在 worker 内收敛，不中断整批。"""
            try:
                tenant = self._ensure_tenant(movie.movie_number)
                self._refresh_movie(movie, tenant=tenant, dry_run=options.dry_run)
                with stats_lock:
                    stats["refreshed_movies"] += 1
                if not options.dry_run:
                    self.ledger.mark_done(movie.id)
            except Exception as exc:  # 单部失败不能中断整批
                with stats_lock:
                    stats["failed_movies"] += 1
                self.ledger.mark_failed(movie.id, f"{type(exc).__name__}:{exc}")
                logger.warning(
                    "Movie cover refresh failed movie_number={} detail={}",
                    movie.movie_number,
                    exc,
                )
            finally:
                with stats_lock:
                    stats["processed_movies"] += 1
                if self.REQUEST_INTERVAL_SECONDS > 0:
                    # 平滑请求速率：并发批会同时打出 concurrency 个请求，这里限一下节奏。
                    time.sleep(self.REQUEST_INTERVAL_SECONDS)

        completed = 0
        batch: list[Movie] = []
        for movie in query.iterator():
            with stats_lock:
                in_flight = stats["processed_movies"] + len(batch)
            if options.limit is not None and in_flight >= options.limit:
                break
            if movie.id in done_ids:
                with stats_lock:
                    stats["skipped_movies"] += 1
                continue
            batch.append(movie)
            if len(batch) >= options.concurrency:
                completed = self._run_batch(batch, handle, completed=completed, emit=emit)
                batch.clear()
        if batch:
            completed = self._run_batch(batch, handle, completed=completed, emit=emit)

        logger.info(
            "Movie cover refresh finished target_movies={} refreshed={} failed={} skipped={}",
            total,
            stats["refreshed_movies"],
            stats["failed_movies"],
            stats["skipped_movies"],
        )
        return stats

    # ------------------------------------------------------------------ 单部刷新

    @staticmethod
    def _run_batch(batch, handle, *, completed: int, emit) -> int:
        """并发跑一批影片；handle 已收敛异常，这里只负责收尾与进度上报。"""
        with ThreadPoolExecutor(
            max_workers=len(batch), thread_name_prefix="cover-refresh"
        ) as executor:
            futures = {executor.submit(handle, movie): movie for movie in batch}
            for future in as_completed(futures):
                movie = futures[future]
                try:
                    future.result()
                except Exception as exc:  # worker 自身崩溃不能拖垮整批
                    logger.error(
                        "Movie cover refresh worker crashed movie_number={} detail={}",
                        movie.movie_number,
                        exc,
                    )
                completed += 1
                emit(completed, movie_number=movie.movie_number)
        return completed

    def _refresh_movie(self, movie: Movie, *, tenant: str, dry_run: bool) -> None:
        cover_task = self._build_cover_task(movie, tenant)
        prepared = self._download_cover(movie, cover_task, tenant=tenant)
        try:
            self._normalize_prepared_payload(prepared[0])
            plot_links = self._plot_links(movie.id)
            thin_cover = self.image_service.resolve_thin_cover_from_prepared_images(
                movie.movie_number, prepared[0].image_task, [], prepared
            )
            if thin_cover.generated_prepared_file is not None:
                prepared.append(thin_cover.generated_prepared_file)
            else:
                # 封面切不出书脊（抽样约 4%）：按现有剧情图回退，不能读旧封面。
                thin_cover = self.image_service.resolve_thin_cover_from_plot_images(
                    plot_links
                )
            if dry_run:
                logger.info(
                    "Movie cover refresh dry-run movie_number={} url={} bytes={} thin_cover={}",
                    movie.movie_number,
                    cover_task.image_url,
                    prepared[0].temp_path.stat().st_size,
                    thin_cover.generated_task is not None
                    or thin_cover.selected_plot_index is not None,
                )
                return
            self.image_service.finalize_prepared_image_files(prepared)
            obsolete_paths = self._persist_cover_records(
                movie.id, prepared[0].image_task, thin_cover, plot_links
            )
        finally:
            self.image_service.cleanup_prepared_image_files(prepared)
        self.image_service.delete_obsolete_image_files(obsolete_paths)
        # 包已成权威：新封面/薄封面先以单文件落盘，重建后进包并清掉单文件。
        if not MovieAssetPackService.rebuild_movie_asset_pack(
            movie_asset_relative_dir(normalize_asset_dir_name(movie.movie_number))
        ):
            # 活跃图片集不稳定（某张图既无单文件也不在包里）时包保持旧内容，
            # 由 backfill-movie-asset-packs 事后收敛；这里只留痕不重试。
            logger.warning(
                "Movie cover refresh pack rebuild skipped movie_number={}",
                movie.movie_number,
            )
        logger.debug(
            "Movie cover refresh persisted movie_number={} cover={} thin_cover={}",
            movie.movie_number,
            cover_task.relative_path,
            thin_cover.generated_task.relative_path
            if thin_cover.generated_task is not None
            else thin_cover.selected_plot_index,
        )

    def _download_cover(
        self, movie: Movie, cover_task: ImagePersistTask, *, tenant: str
    ) -> list[PreparedImageFile]:
        """下载封面；推导地址 404 时按 provider 的真实地址重试一次（租户段可能已换）。"""
        try:
            return self.image_service.download_image_tasks_to_temporary_files([cover_task])
        except ImageDownloadError as exc:
            if "unexpected_status_code:404" not in str(exc):
                raise
            refreshed_tenant = self._ensure_tenant(movie.movie_number, force=True)
            if refreshed_tenant == tenant:
                raise
            retry_task = self._build_cover_task(movie, refreshed_tenant)
            logger.info(
                "Movie cover refresh retried with new tenant movie_number={} tenant={}",
                movie.movie_number,
                refreshed_tenant,
            )
            return self.image_service.download_image_tasks_to_temporary_files([retry_task])

    def _build_cover_task(self, movie: Movie, tenant: str) -> ImagePersistTask:
        """构造封面落盘任务；沿用现有封面的后缀，避免 origin 抖动与格式回退。"""
        cover_url = self.app_cover_url(movie.javdb_id, tenant)
        cover_task = self.image_service.build_movie_import_image_tasks(
            movie.movie_number, cover_url, [], ()
        )[0]
        if cover_task is None:
            raise ImageDownloadError(f"cover_task_missing:{movie.movie_number}")
        current_suffix = Path(self._cover_origin(movie.id) or "").suffix.lower()
        if not current_suffix or current_suffix == Path(cover_task.relative_path).suffix.lower():
            return cover_task
        relative_path = Path(cover_task.relative_path).with_suffix(current_suffix).as_posix()
        return ImagePersistTask(
            image_type=cover_task.image_type,
            image_url=cover_task.image_url,
            relative_path=relative_path,
            absolute_path=media_image_root_path() / relative_path,
        )

    @staticmethod
    def _normalize_prepared_payload(prepared_file: PreparedImageFile) -> None:
        """把下载字节对齐到目标后缀：格式一致就复用原始字节，不一致才重编码。"""
        suffix = Path(prepared_file.image_task.relative_path).suffix.lower()
        payload = prepared_file.temp_path.read_bytes()
        try:
            with PillowImage.open(BytesIO(payload)) as image:
                image.load()
                detected = image.format
        except (UnidentifiedImageError, OSError, PillowImage.DecompressionBombError) as exc:
            raise ImageDownloadError(
                f"image_decode_failed:{prepared_file.image_task.image_url}:{exc}"
            ) from exc
        if suffix in FORMAT_SUFFIXES.get(detected or "", frozenset()):
            return
        prepared_file.temp_path.write_bytes(_encode_payload(payload, suffix))

    @staticmethod
    def _plot_links(movie_id: int) -> list[MoviePlotImage]:
        return list(
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie_id)
            .order_by(MoviePlotImage.id)
        )

    def _persist_cover_records(
        self,
        movie_id: int,
        cover_task: ImagePersistTask,
        thin_cover: ThinCoverResolution,
        plot_links: list[MoviePlotImage],
    ) -> set[str]:
        """事务内切换封面/薄封面引用，返回待清理的旧图路径。"""
        has_thin_cover = (
            thin_cover.generated_task is not None
            or thin_cover.selected_plot_index is not None
        )
        obsolete_paths: set[str] = set()
        with get_database().atomic():
            movie = Movie.select().where(Movie.id == movie_id).for_update().get()
            old_cover = movie.cover_image
            old_thin_cover = movie.thin_cover_image
            movie.cover_image = None
            movie.save(only=[Movie.cover_image])
            if old_thin_cover is not None and has_thin_cover:
                movie.thin_cover_image = None
                movie.save(only=[Movie.thin_cover_image])
            # 宿主整批刷新后封面回到自动管理：清掉插件/人工留下的图片 owner。
            MovieOwnershipGateway.release_image_owners(movie.id, ("cover_image",))
            movie.cover_image = self.image_service.persist_refreshed_image_record(cover_task)
            movie.save(only=[Movie.cover_image])
            if has_thin_cover:
                obsolete_paths.update(
                    apply_thin_cover_resolution(
                        self.image_service,
                        movie,
                        old_thin_cover,
                        thin_cover,
                        {index: link.image for index, link in enumerate(plot_links)},
                        refreshed=True,
                    )
                )
            else:
                # 封面与剧情图都没解析出薄封面：保留旧薄封面，不做降级删除。
                logger.warning(
                    "Movie cover refresh kept existing thin cover movie_id={} thin_cover={}",
                    movie_id,
                    old_thin_cover.origin if old_thin_cover is not None else None,
                )
            new_cover = movie.cover_image
            if old_cover is not None and (new_cover is None or old_cover.id != new_cover.id):
                obsolete_paths.update(
                    self.image_service.delete_image_record_if_unused(old_cover)
                )
        return obsolete_paths

    # ------------------------------------------------------------------ 租户解析

    def _ensure_tenant(self, movie_number: str, *, force: bool = False) -> str:
        with self._tenant_lock:
            if self._tenant is None or force:
                self._tenant = self._resolve_tenant(movie_number)
            return self._tenant

    def _resolve_tenant(self, movie_number: str) -> str:
        """从 provider 详情里取 app CDN 租户段；每次任务只取一次。"""
        detail = self.provider.get_movie_by_number(movie_number)
        cover_url = getattr(detail, "cover_image", None) or ""
        match = self.TENANT_PATTERN.match(cover_url)
        if match is None:
            raise RuntimeError(
                f"JavDB 详情未返回 app CDN 封面地址 movie_number={movie_number} cover_url={cover_url}"
            )
        return match.group("tenant")


def _encode_payload(payload: bytes, suffix: str) -> bytes:
    """按目标后缀重编码图片字节；不支持的后缀直接失败，不写错格式的文件。"""
    pillow_format = PILLOW_FORMATS.get(suffix)
    if pillow_format is None:
        raise ImageDownloadError(f"unsupported_image_suffix:{suffix}")
    with PillowImage.open(BytesIO(payload)) as image:
        image = ImageOps.exif_transpose(image)
        if pillow_format == "JPEG" and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output = BytesIO()
        if pillow_format == "JPEG":
            image.save(output, format=pillow_format, quality=JPEG_QUALITY)
        elif pillow_format == "WEBP":
            image.save(output, format=pillow_format, quality=WEBP_QUALITY)
        else:
            image.save(output, format=pillow_format)
    return output.getvalue()
