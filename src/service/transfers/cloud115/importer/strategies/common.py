"""cloud115 导入策略 (copy / move) 共用的原子操作。

拆自 ``service.py``：这些 helper 被 copy 和 move 两条策略共同调用，抽到模块级自由
函数后策略实现可以进一步拆到独立文件（copy.py / move.py），而不用互相持有 service
实例。

- ``record_files_failure``：批量记账失败项（copy 侧的 group 版包在 copy.py 里）。
- ``probe_cloud115_media``：字幕/元数据探测的薄包装，只是在 ``common.probe_cloud115_media``
  基础上多打一条 info 日志。
- ``register_media``：sha1 幂等登记一条 cloud115 Media；返回 ``(media, 是否新登记)``。
- ``import_subtitle``：把配对 .srt 下载到本地并落 Subtitle 表。
"""

from __future__ import annotations

from loguru import logger

from src.common.media_import_status import make_failure_item
from src.common.media_paths import allocate_next_movie_subtitle_path, movie_subtitle_dir
from src.lib.cloud115 import Cloud115Client
from src.model import Media, MediaLibrary, Movie, Subtitle
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.cloud115.importer.common import (
    CLOUD115_METADATA_PROBE_MAX_BYTES,
    probe_cloud115_media as _probe_cloud115_media_raw,
)
from src.service.transfers.cloud115.importer.media_registrar import Cloud115MediaRegistrar
from src.service.transfers.cloud115.importer.types import CloudSourceFile
from src.service.transfers.downloads.guards.tag_rules import build_media_special_tags

# 直链下载字幕的 UA（拿链接与 GET 由 SDK 保证同 UA）。
SUBTITLE_DOWNLOAD_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Import/1.0"
# 字幕文件大小上限：.srt 纯文本，10MB 足够富余。
SUBTITLE_MAX_BYTES = 10 * 1024 * 1024


def record_files_failure(
    files: list[CloudSourceFile],
    *,
    reason: str,
    detail: str,
    failure_items: list[dict],
    stats: dict,
    kind: str | None = None,
) -> None:
    """给一批文件各记一条失败；kind 用于覆盖 reason 的默认可操作性分类。"""
    for cloud_file in files:
        stats["failed"] += 1
        item = make_failure_item(cloud_file.rel_path, reason, detail)
        if kind is not None:
            item["kind"] = kind
        failure_items.append(item)


async def probe_cloud115_media(
    client: Cloud115Client,
    probe_service: MediaMetadataProbeService,
    *,
    pickcode: str,
    file_size_bytes: int,
) -> MediaMetadataProbeResult:
    metadata, fetched_bytes = await _probe_cloud115_media_raw(
        client,
        probe_service,
        pickcode=pickcode,
        file_size_bytes=file_size_bytes,
    )
    logger.info(
        "Cloud115 metadata probed pickcode={} fetched_bytes={} budget_bytes={}",
        pickcode, fetched_bytes, CLOUD115_METADATA_PROBE_MAX_BYTES,
    )
    return metadata


def register_media(
    *,
    library: MediaLibrary,
    movie: Movie,
    cloud_file: CloudSourceFile,
    target_fid: str,
    target_pickcode: str,
    encoded_name: str,
    metadata: MediaMetadataProbeResult | None,
) -> tuple[Media, bool]:
    """按 sha1 指纹幂等登记一条 cloud115 Media。

    返回 ``(media, 是否新登记)``；False 表示命中已有有效记录、只更新了定位信息。
    调用方需要 media 实例来做搬运后的定位修正，因此一并返回。
    """
    locator = Cloud115MediaRegistrar.build_locator(
        fid=target_fid,
        pickcode=target_pickcode,
        name=encoded_name,
        source_path=cloud_file.rel_path,
    )
    fingerprint = Cloud115MediaRegistrar.build_fingerprint(cloud_file.sha1)
    valid = not cloud_file.censored

    existing_valid = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=True)
    effective_video_info = metadata.video_info if metadata is not None else None
    if existing_valid is not None and effective_video_info is None:
        effective_video_info = existing_valid.video_info
    if valid and effective_video_info is None:
        raise RuntimeError("valid cloud115 media requires video_info")
    resolution = metadata.resolution if metadata is not None else None
    if metadata is not None:
        duration_seconds = metadata.duration_seconds or cloud_file.play_long or 0
    elif existing_valid is not None:
        duration_seconds = existing_valid.duration_seconds or cloud_file.play_long or 0
    else:
        duration_seconds = cloud_file.play_long or 0
    special_tags = build_media_special_tags(
        [cloud_file.rel_path],
        movie.movie_number,
        video_info=effective_video_info,
        has_subtitle=cloud_file.subtitle is not None,
    )

    if existing_valid is not None:
        previous_locator = existing_valid.backend_locator or {}
        locator["source_path"] = previous_locator.get("source_path") or cloud_file.rel_path
        Cloud115MediaRegistrar.apply_cloud115_fields(
            existing_valid,
            library=library,
            locator=locator,
            fingerprint=fingerprint,
            file_size_bytes=cloud_file.size,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_info=effective_video_info,
            special_tags=special_tags,
        )
        existing_valid.save()
        return existing_valid, False

    invalid_media = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=False)
    if invalid_media is not None:
        invalid_media.movie = movie
        Cloud115MediaRegistrar.apply_cloud115_fields(
            invalid_media,
            library=library,
            locator=locator,
            fingerprint=fingerprint,
            file_size_bytes=cloud_file.size,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_info=effective_video_info,
            special_tags=special_tags,
            valid=valid,
        )
        invalid_media.save()
        if valid:
            Cloud115MediaRegistrar.reset_thumbnail_state(invalid_media.id)
        return invalid_media, True

    media = Cloud115MediaRegistrar.create_cloud115_media(
        movie=movie,
        library=library,
        locator=locator,
        fingerprint=fingerprint,
        file_size_bytes=cloud_file.size,
        resolution=resolution,
        duration_seconds=duration_seconds,
        video_info=effective_video_info,
        special_tags=special_tags,
        valid=valid,
    )
    if valid:
        Cloud115MediaRegistrar.reset_thumbnail_state(media.id)
    logger.info(
        "Cloud115 media registered movie_number={} media_id={} pickcode={} name={}",
        movie.movie_number, media.id, target_pickcode, encoded_name,
    )
    return media, True


async def import_subtitle(
    client: Cloud115Client,
    *,
    movie: Movie,
    cloud_file: CloudSourceFile,
) -> None:
    """把配对的 .srt 下载到 movies/<shard>/{番号}/subtitles/<番号>-<N>.srt 并登记 Subtitle。

    字幕不复制到 115（库子树只存影片文件）；删除源字幕由整组成功后的清源阶段统一处理。
    命名与本地导入 / 迁移共用 ``allocate_next_movie_subtitle_path`` 分配的 ``<番号>-<N>.srt``。
    重跑幂等：源字幕在 115 上的 pickcode 稳定，下载内容一致；由外层组级 cleanup-source 保证
    成功一次后源字幕被删除，重跑不会重复下载。清源前中断重跑会重复下载一次（在同一部影片下
    分到新的 N），可后续由用户或 sync 收敛。
    """
    subtitle = cloud_file.subtitle
    assert subtitle is not None
    subtitle_dir = movie_subtitle_dir(movie.movie_number)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    target_path = allocate_next_movie_subtitle_path(movie.movie_number)
    content = await client.download_bytes(
        subtitle.pickcode,
        user_agent=SUBTITLE_DOWNLOAD_UA,
        max_bytes=SUBTITLE_MAX_BYTES,
    )
    target_path.write_bytes(content)
    Subtitle.get_or_create(movie=movie, file_path=str(target_path))
