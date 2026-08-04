"""cloud115 导入的 cleanup-source (move) 策略：源直接移动进库，不复制、不留副本。

拆自 ``service._import_group_by_move`` + 三个 move-only 辅助（_cleanup_duplicate_source /
_discard_version_dirs / _restore_locator_name）。

关键差异（与 copy 相对照，见文件顶部 service.py 注释）：
- 115 的 move 保持 fid/pickcode 不变，Media 只靠 pickcode 定位，登记先于搬运；
- 幂等靠"源还在不在"判断：本轮扫得到即需搬运，扫不到说明已搬走；
- 顺序固定：探测(源 pickcode) → 字幕 → 建版本目录 → 登记 Media → move → rename。
"""

from __future__ import annotations

import time

from loguru import logger

from src.common.media_import_status import (
    FAILED_FILE_KIND_FILE,
    FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
    FAILURE_REASON_CLOUD115_RENAME_FAILED,
    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_SOURCE_DELETE_FAILED,
    make_failure_item,
)
from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import Cloud115Client
from src.model import Media, MediaLibrary, Movie, get_database
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.cloud115.importer.common import (
    Cloud115TargetDirResolver,
    normalize_jav_media_filename,
    probe_cloud115_media,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115.importer.media_registrar import Cloud115MediaRegistrar
from src.service.transfers.cloud115.importer.strategies.common import (
    import_subtitle,
    record_files_failure,
    register_media,
)
from src.service.transfers.cloud115.importer.types import CloudImportGroup, CloudSourceFile


async def _cleanup_duplicate_source(
    client: Cloud115Client,
    *,
    movie: Movie,
    cloud_file: CloudSourceFile,
    failure_items: list[dict],
    stats: dict,
) -> None:
    """库内已有该内容的独立副本时，只清掉这一份多余的源（含配对字幕）。

    字幕先下载到本地再删远端；下载失败就整份保留不删，让用户可以重导。
    """
    if cloud_file.subtitle is not None:
        try:
            await import_subtitle(
                client,
                movie=movie,
                cloud_file=cloud_file,
            )
        except Exception as exc:
            stats["failed"] += 1
            item = make_failure_item(
                cloud_file.rel_path,
                FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                str(exc),
            )
            item["kind"] = FAILED_FILE_KIND_FILE
            failure_items.append(item)
            logger.warning(
                "Cloud115 duplicate source subtitle failed rel_path={} detail={}",
                cloud_file.rel_path, exc,
            )
            return

    fids = [cloud_file.fid]
    if cloud_file.subtitle is not None:
        fids.append(cloud_file.subtitle.fid)
    try:
        await client.delete_files(fids)
    except Exception as exc:
        stats["failed"] += 1
        # 源还在，重导可以再试一次删除。
        item = make_failure_item(
            cloud_file.rel_path, FAILURE_REASON_SOURCE_DELETE_FAILED, str(exc)
        )
        item["kind"] = FAILED_FILE_KIND_FILE
        failure_items.append(item)
        logger.warning(
            "Cloud115 duplicate source delete failed rel_path={} detail={}",
            cloud_file.rel_path, exc,
        )


async def _discard_version_dirs(
    client: Cloud115Client,
    version_cids,
    *,
    movie_number: str,
) -> None:
    """回收本轮新建、最终没用上的空版本目录（走回收站）；失败仅告警。"""
    for version_cid in version_cids:
        try:
            await client.delete_files([version_cid])
        except Exception as exc:
            logger.warning(
                "Cloud115 version dir discard failed movie_number={} version_cid={} detail={}",
                movie_number, version_cid, exc,
            )


def _restore_locator_name(media: Media, actual_name: str) -> None:
    """改名失败时把 locator.name 回写为 115 上的实际名，保持记录与远端一致。"""
    locator = dict(media.backend_locator or {})
    if locator.get("name") == actual_name:
        return
    locator["name"] = actual_name
    media.backend_locator = locator
    media.updated_at = utc_now_for_db()
    media.save()


async def import_group_by_move(
    client: Cloud115Client,
    *,
    library: MediaLibrary,
    movie: Movie,
    group: CloudImportGroup,
    target_dir_resolver: Cloud115TargetDirResolver,
    failure_items: list[dict],
    stats: dict,
    new_playable_movies: dict[int, dict],
    probe_service: MediaMetadataProbeService,
) -> None:
    """把源文件真正移动进 ``jav/{番号}/{版本ms}/{番号}{ext}``，不复制也不删源。

    115 的 move 保持 fid / pickcode 不变，而 cloud115 Media 只靠 pickcode 定位、
    与文件所在目录无关，所以登记可以**先于**搬运完成。顺序固定为：
    探测(源 pickcode) → 字幕 → 建版本目录 → 登记 Media → move → rename。
    依赖源文件的步骤全部前置，于是任一步失败时只有两种局面：源原地未动（可完整
    重导），或文件已在受管目录且 Media 有效（可播）。不会出现"文件已搬进库、
    却没有任何记录"的孤儿——移动语义下源已消失，那种孤儿是无法自动收回的。
    """
    # 1) 按库内已有记录分流。fid 相同说明登记的就是这个源文件本身（上一轮登记
    # 成功但搬运没走完），必须继续搬运；只有 fid 不同才是真正多余的副本，删源。
    pending: list[CloudSourceFile] = []
    # 续做搬运的文件：Media 不是新建的，但本轮确实把它搬进了库，统计上算导入成功，
    # 否则用户重导后会看到"跳过"，与他刚刚完成的重试动作对不上。
    resuming_fids: set[str] = set()
    for cloud_file in group.files:
        existing_valid = Cloud115MediaRegistrar.find_library_media(
            library, cloud_file.sha1, valid=True
        )
        if existing_valid is None:
            pending.append(cloud_file)
            continue
        registered_fid = str(
            (existing_valid.backend_locator or {}).get("fid") or ""
        )
        if registered_fid == cloud_file.fid:
            pending.append(cloud_file)
            resuming_fids.add(cloud_file.fid)
            continue
        stats["skipped"] += 1
        failure_items.append(
            make_failure_item(
                cloud_file.rel_path,
                FAILURE_REASON_DUPLICATE_FINGERPRINT,
                f"库中已存在相同内容（media_id={existing_valid.id}）",
            )
        )
        await _cleanup_duplicate_source(
            client,
            movie=movie,
            cloud_file=cloud_file,
            failure_items=failure_items,
            stats=stats,
        )

    if not pending:
        return

    # 2) 探测用源 pickcode：move 不改 pickcode，与搬运后探测等价，但失败时源未动。
    probe_results: dict[str, MediaMetadataProbeResult | None] = {}
    for cloud_file in pending:
        existing_valid = Cloud115MediaRegistrar.find_library_media(
            library, cloud_file.sha1, valid=True
        )
        if cloud_file.censored or (
            existing_valid is not None and existing_valid.video_info is not None
        ):
            probe_results[cloud_file.sha1] = None
            continue
        try:
            metadata, _fetched = await probe_cloud115_media(
                client,
                probe_service,
                pickcode=cloud_file.pickcode,
                file_size_bytes=cloud_file.size,
            )
            probe_results[cloud_file.sha1] = metadata
        except Exception as exc:
            record_files_failure(
                pending,
                reason=FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
                detail=f"{cloud_file.rel_path}: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.warning(
                "Cloud115 metadata probe failed movie_number={} rel_path={} detail={}",
                group.movie_number, cloud_file.rel_path, exc,
            )
            return

    # 3) 字幕同样前置：源一旦移走就无法重导，字幕失败必须在源完好时终止整组。
    for cloud_file in pending:
        if cloud_file.subtitle is None:
            continue
        try:
            await import_subtitle(
                client,
                movie=movie,
                cloud_file=cloud_file,
            )
        except Exception as exc:
            record_files_failure(
                pending,
                reason=FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                detail=f"{cloud_file.rel_path}: {exc}",
                failure_items=failure_items,
                stats=stats,
                kind=FAILED_FILE_KIND_FILE,
            )
            logger.warning(
                "Cloud115 subtitle download failed movie_number={} rel_path={} detail={}",
                group.movie_number, cloud_file.rel_path, exc,
            )
            return

    # 4) 解析番号目录并为每个文件建独立版本目录。移动模式不需要读目标目录对账：
    # 幂等靠"源还在不在"判断，源已移走的文件本轮根本扫不到。
    try:
        entity_cid, _ = await target_dir_resolver.resolve_jav_entity(
            group.movie_number
        )
    except Exception as exc:
        record_files_failure(
            pending,
            reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
            detail=f"创建番号目录失败: {exc}",
            failure_items=failure_items,
            stats=stats,
        )
        logger.exception(
            "Cloud115 entity dir failed movie_number={} detail={}",
            group.movie_number, exc,
        )
        return

    version_cids: dict[str, str] = {}
    try:
        for cloud_file in pending:
            version_cids[cloud_file.fid] = (
                await target_dir_resolver.create_version_dir(
                    entity_cid=entity_cid,
                    now_ms=int(time.time() * 1000),
                )
            )
    except Exception as exc:
        # 新建的版本目录本轮独占，回收掉避免在番号目录下累积空目录；
        # entity_cid 可能被同番号其它导入共享，不动。
        await _discard_version_dirs(
            client, version_cids.values(), movie_number=group.movie_number
        )
        record_files_failure(
            pending,
            reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
            detail=f"创建版本目录失败: {exc}",
            failure_items=failure_items,
            stats=stats,
        )
        logger.exception(
            "Cloud115 version dir failed movie_number={} detail={}",
            group.movie_number, exc,
        )
        return

    # 5) 整组 Media 在同一事务登记，locator 直接用源 fid/pickcode（move 不改这两个值）。
    registered: list[tuple[CloudSourceFile, Media, bool]] = []
    try:
        with get_database().atomic():
            for cloud_file in pending:
                media, is_new = register_media(
                    library=library,
                    movie=movie,
                    cloud_file=cloud_file,
                    target_fid=cloud_file.fid,
                    target_pickcode=cloud_file.pickcode,
                    encoded_name=normalize_jav_media_filename(
                        group.movie_number, cloud_file.name
                    ),
                    metadata=probe_results[cloud_file.sha1],
                )
                registered.append((cloud_file, media, is_new))
    except Exception as exc:
        await _discard_version_dirs(
            client, version_cids.values(), movie_number=group.movie_number
        )
        record_files_failure(
            pending,
            reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
            detail=str(exc),
            failure_items=failure_items,
            stats=stats,
        )
        logger.exception(
            "Cloud115 media register failed movie_number={} detail={}",
            group.movie_number, exc,
        )
        return

    # 6) 搬运。失败时源原地未动、Media 已登记且可播，失败项可重导——重导会命中
    # 步骤 1 的 fid 相同分支，补完搬运即收敛。
    moved: list[tuple[CloudSourceFile, Media, bool]] = []
    for cloud_file, media, is_new in registered:
        try:
            await client.move_files(
                [cloud_file.fid], pid=version_cids[cloud_file.fid]
            )
        except Exception as exc:
            await _discard_version_dirs(
                client,
                [version_cids[cloud_file.fid]],
                movie_number=group.movie_number,
            )
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    str(exc),
                )
            )
            logger.warning(
                "Cloud115 move failed movie_number={} rel_path={} detail={}",
                group.movie_number, cloud_file.rel_path, exc,
            )
            continue
        moved.append((cloud_file, media, is_new))

    # 7) 改名 + 校验，并结算已搬运文件的统计。
    for cloud_file, media, is_new in moved:
        target_name = normalize_jav_media_filename(
            group.movie_number, cloud_file.name
        )
        if cloud_file.name != target_name:
            try:
                await client.rename_file(cloud_file.fid, target_name)
                await verify_cloud115_renamed_file(
                    client, cloud_file.fid, target_name
                )
            except Exception as exc:
                # 文件已在受管目录、Media 可播，源已不存在，重导无从下手：
                # 把 locator.name 回写成 115 上的实际名保持一致，并降为告警项。
                _restore_locator_name(media, cloud_file.name)
                item = make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_CLOUD115_RENAME_FAILED,
                    str(exc),
                )
                item["kind"] = FAILED_FILE_KIND_WARNING
                failure_items.append(item)
                logger.warning(
                    "Cloud115 rename verification failed movie_number={} fid={} detail={}",
                    group.movie_number, cloud_file.fid, exc,
                )
        if cloud_file.censored:
            # 违规文件按 invalid 登记完成，报告但不计入可播放。
            stats["imported"] += 1
            failure_items.append(
                make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_CLOUD115_FILE_CENSORED,
                    "115 标记该文件违规（ic=1）",
                )
            )
        elif is_new or cloud_file.fid in resuming_fids:
            stats["imported"] += 1
            new_playable_movies[movie.id] = {
                "movie_id": movie.id,
                "movie_number": movie.movie_number,
                "title": movie.title,
            }
        else:
            stats["skipped"] += 1

    # 8) 视频本身已经移走，只剩配对字幕的远端源要清理（本地副本已落盘）。
    # 清不掉只残留一个 .srt，且源视频已不在、无法通过重导再试，只作告警。
    for cloud_file, _media, _is_new in moved:
        if cloud_file.subtitle is None:
            continue
        try:
            await client.delete_files([cloud_file.subtitle.fid])
        except Exception as exc:
            failure_items.append(
                make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_SOURCE_DELETE_FAILED,
                    f"源字幕删除失败: {exc}",
                )
            )
            logger.warning(
                "Cloud115 source subtitle delete failed movie_number={} rel_path={} detail={}",
                group.movie_number, cloud_file.rel_path, exc,
            )
