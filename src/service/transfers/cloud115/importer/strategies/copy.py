"""cloud115 导入的 copy 策略：复制源文件到库、源始终保留。

拆自 ``service._import_group_by_copy``。整块流程与 move 策略语义完全不同，独立成文件
避免和 move 策略挤在同一个 1000+ 行的 service 里。

关键差异（与 move 相对照）：
- 复制产生新 fid/pickcode，登记以复制后 re-list 目标目录的对账结果为准；
- 幂等靠「目标目录 sha1 对账」收敛：已搬的跳过搬运、没改名的补改名、没登记的补登记；
- 源文件始终保留，字幕失败只作告警。
"""

from __future__ import annotations

import time

from loguru import logger

from src.common.media_import_status import (
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
    FAILURE_REASON_CLOUD115_RENAME_FAILED,
    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    make_failure_item,
)
from src.lib.cloud115 import Cloud115Client, DirEntry
from src.model import Media, MediaLibrary, Movie, get_database
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.cloud115.importer.common import (
    Cloud115TargetDirResolver,
    list_cloud115_entity_target_files,
    list_cloud115_target_files,
    normalize_jav_media_filename,
    resolve_cloud115_copied_entry,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115.importer.media_registrar import Cloud115MediaRegistrar
from src.service.transfers.cloud115.importer.strategies.common import (
    import_subtitle,
    probe_cloud115_media,
    record_files_failure,
    register_media,
)
from src.service.transfers.cloud115.importer.types import (
    CloudImportGroup,
    CloudSourceFile,
    ResolvedFile,
)


def _record_group_failure(
    group: CloudImportGroup,
    *,
    reason: str,
    detail: str,
    failure_items: list[dict],
    stats: dict,
) -> None:
    record_files_failure(
        group.files,
        reason=reason,
        detail=detail,
        failure_items=failure_items,
        stats=stats,
    )


def _resolve_registered_entry(candidates: list[DirEntry], media: Media) -> DirEntry | None:
    """优先按数据库已有 fid/pickcode 找回受管目标，避免同 SHA 条目之间误切换。"""
    locator = media.backend_locator or {}
    fid = str(locator.get("fid") or "")
    pickcode = str(locator.get("pickcode") or "")
    for candidate in candidates:
        if fid and candidate.entry_id == fid:
            return candidate
    for candidate in candidates:
        if pickcode and candidate.pickcode == pickcode:
            return candidate
    return None


async def import_group_by_copy(
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
    """复制并登记一个番号组；任一改名失败时整组不入库。源文件始终保留。

    目标结构对齐本地：``jav/{番号}/{版本ms}/{番号}{ext}``。同番号多分部共享
    实体目录，各自版本子目录；中断恢复时按番号目录做 sha1 对账，命中即复用远端
    已复制条目，不重复复制。
    """
    try:
        entity_cid, entity_created = (
            await target_dir_resolver.resolve_jav_entity(group.movie_number)
        )
    except Exception as exc:
        _record_group_failure(
            group,
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

    # 1) 预扫番号目录下所有版本子目录里的 sha1，用于中断恢复对账。
    try:
        entity_entries_by_sha1 = (
            {}
            if entity_created
            else await list_cloud115_entity_target_files(client, entity_cid)
        )
    except Exception as exc:
        _record_group_failure(
            group,
            reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
            detail=f"列出番号目录失败: {exc}",
            failure_items=failure_items,
            stats=stats,
        )
        logger.exception(
            "Cloud115 entity list failed movie_number={} detail={}",
            group.movie_number, exc,
        )
        return
    preexisting_target_sha1 = set(entity_entries_by_sha1)

    # 1b) 主动清理多余候选：同一 sha1 在实体目录下有多个副本，说明是历史 job 崩溃
    # 或重跑遗留的脏数据。保留 mtime 最新的作为权威条目，其余进回收站；
    # 删失败仅告警不中断本次导入（下次运行时可再次尝试）。
    # entity 目录完全由 SakuraMedia 管，用户不会往里塞文件，主动收敛安全。
    for sha1, candidates in list(entity_entries_by_sha1.items()):
        if len(candidates) <= 1:
            continue
        winner = max(candidates, key=lambda entry: entry.mtime)
        stale = [entry for entry in candidates if entry.entry_id != winner.entry_id]
        entity_entries_by_sha1[sha1] = [winner]
        stale_fids = [entry.entry_id for entry in stale]
        try:
            await client.delete_files(stale_fids)
            logger.warning(
                "Cloud115 entity stale duplicates cleaned movie_number={} sha1={} kept_fid={} kept_mtime={} deleted_fids={}",
                group.movie_number, sha1, winner.entry_id, winner.mtime, stale_fids,
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Cloud115 entity stale duplicates cleanup failed movie_number={} sha1={} stale_fids={} detail={}",
                group.movie_number, sha1, stale_fids, cleanup_exc,
            )

    # 2) 逐文件处理：sha1 已在实体下则复用；否则新建版本目录 + 复制。
    resolved: list[ResolvedFile] = []
    for cloud_file in group.files:
        target_name = normalize_jav_media_filename(
            group.movie_number, cloud_file.name
        )
        existing_valid = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=True)
        reuse_managed_target = (
            existing_valid is not None
            and cloud_file.sha1 in preexisting_target_sha1
        )
        target_entry: DirEntry | None = None
        if cloud_file.sha1 in entity_entries_by_sha1:
            candidates = entity_entries_by_sha1[cloud_file.sha1]
            if reuse_managed_target:
                target_entry = _resolve_registered_entry(
                    candidates, existing_valid
                )
            target_entry = target_entry or candidates[0]
        else:
            # 需要新复制：为本文件建独立版本目录，跟本地 create_version_directory 对齐。
            version_cid: str | None = None
            try:
                version_cid = await target_dir_resolver.create_version_dir(
                    entity_cid=entity_cid,
                    now_ms=int(time.time() * 1000),
                )
                await client.copy_files([cloud_file.fid], pid=version_cid)
                version_entries = await list_cloud115_target_files(client, version_cid)
            except Exception as exc:
                # copy/list 失败时，version_cid 是本次新建的独占目录，回收避免
                # 在 jav/{番号}/ 下累积空/半成品目录（走回收站有缓冲）。
                # entity_cid 可能被同番号其它导入共享，此处不动。
                if version_cid is not None:
                    try:
                        await client.delete_files([version_cid])
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Cloud115 version dir rollback failed movie_number={} version_cid={} detail={}",
                            group.movie_number, version_cid, cleanup_exc,
                        )
                _record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    detail=str(exc),
                    failure_items=failure_items,
                    stats=stats,
                )
                logger.exception(
                    "Cloud115 copy failed movie_number={} detail={}",
                    group.movie_number, exc,
                )
                return
            target_entry = resolve_cloud115_copied_entry(
                version_entries, cloud_file, target_name
            )
            if target_entry is not None:
                # 让同批次后续同 sha1 命中能看到刚复制的条目，避免二次 copy。
                entity_entries_by_sha1[cloud_file.sha1] = [target_entry]

        if target_entry is None:
            _record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=f"复制后在目标目录未找到 sha1={cloud_file.sha1} 的条目",
                failure_items=failure_items,
                stats=stats,
            )
            return
        resolved.append(ResolvedFile(
            cloud_file=cloud_file,
            target_fid=target_entry.entry_id,
            target_pickcode=target_entry.pickcode,
            current_name=target_entry.name,
            # 复用已登记条目时保留原名，不改动已在库的命名；新复制统一规范名。
            target_name=target_entry.name if reuse_managed_target else target_name,
        ))

    # 3) 逐文件改名；每次请求后按 fid 查询实际名称，失败立即终止整组。
    for r in resolved:
        if r.current_name == r.target_name:
            continue
        try:
            await client.rename_file(r.target_fid, r.target_name)
            await verify_cloud115_renamed_file(client, r.target_fid, r.target_name)
        except Exception as exc:
            _record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_RENAME_FAILED,
                detail=f"fid={r.target_fid}: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.warning(
                "Cloud115 rename verification failed movie_number={} fid={} detail={}",
                group.movie_number, r.target_fid, exc,
            )
            return

    # 4) 在数据库事务外探测最终受管文件。有效媒体必须带完整技术元数据入库；
    # 已登记且已有 video_info 的清源重试直接复用，避免重复读取远端文件。
    probe_results: dict[str, MediaMetadataProbeResult | None] = {}
    for r in resolved:
        existing_valid = Cloud115MediaRegistrar.find_library_media(library, r.cloud_file.sha1, valid=True)
        if r.cloud_file.censored or (
            existing_valid is not None and existing_valid.video_info is not None
        ):
            probe_results[r.cloud_file.sha1] = None
            continue
        try:
            probe_results[r.cloud_file.sha1] = await probe_cloud115_media(
                client,
                probe_service,
                pickcode=r.target_pickcode,
                file_size_bytes=r.cloud_file.size,
            )
        except Exception as exc:
            _record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
                detail=f"{r.cloud_file.rel_path}: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.warning(
                "Cloud115 metadata probe failed movie_number={} rel_path={} detail={}",
                group.movie_number, r.cloud_file.rel_path, exc,
            )
            return

    # 5) 整组 Media 在同一事务登记；任一失败回滚整组。
    registration_results: list[tuple[CloudSourceFile, bool]] = []
    try:
        with get_database().atomic():
            for r in resolved:
                _media, registered = register_media(
                    library=library,
                    movie=movie,
                    cloud_file=r.cloud_file,
                    target_fid=r.target_fid,
                    target_pickcode=r.target_pickcode,
                    encoded_name=r.target_name,
                    metadata=probe_results[r.cloud_file.sha1],
                )
                registration_results.append((r.cloud_file, registered))
    except Exception as exc:
        _record_group_failure(
            group,
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

    for cloud_file, registered in registration_results:
        if cloud_file.censored:
            # 违规文件按 invalid 登记完成，报告但不计入可播放。
            stats["imported"] += 1
            failure_items.append(
                make_failure_item(
                    cloud_file.rel_path, FAILURE_REASON_CLOUD115_FILE_CENSORED,
                    "115 标记该文件违规（ic=1）",
                )
            )
        elif registered:
            stats["imported"] += 1
            new_playable_movies[movie.id] = {
                "movie_id": movie.id,
                "movie_number": movie.movie_number,
                "title": movie.title,
            }
        else:
            stats["skipped"] += 1

    # 6) 字幕下载到本地；copy 模式源文件始终保留，字幕失败只作告警。
    for r in resolved:
        if r.cloud_file.subtitle is None:
            continue
        try:
            await import_subtitle(
                client,
                movie=movie,
                cloud_file=r.cloud_file,
            )
        except Exception as exc:
            failure_items.append(
                make_failure_item(
                    r.cloud_file.rel_path,
                    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                    str(exc),
                )
            )
            logger.warning(
                "Cloud115 subtitle download failed movie_number={} rel_path={} detail={}",
                group.movie_number, r.cloud_file.rel_path, exc,
            )
