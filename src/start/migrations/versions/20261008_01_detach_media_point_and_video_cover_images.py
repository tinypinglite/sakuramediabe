"""时刻钉图与视频自选封面：从缩略图存储解绑为用户资产。

历史上 ``MediaPoint.image`` / ``VideoItem.cover_image`` 直接引用缩略图的 Image
行，字节存放于该媒体的 ``thumbnails/`` 目录或 ``thumbnails.zip``；缩略图整体重
建（force reset 后的重新生成）会撞 ``image.origin`` 唯一约束。本迁移把这两类
用户资产逐张拷贝为自有松散文件并回写引用：时刻图落 ``media_points/``，视频封面
落 ``videos/<id>/cover/``；旧 Image 行再无其它引用时连同文件一并回收。规则与
迁移逻辑冻结在此，不 import 运行时 service。
"""

from __future__ import annotations

import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from loguru import logger

from src.common.image_store import write_pack
from src.common.media_paths import (
    MEDIA_POINTS_SUBDIR,
    MEDIA_THUMBNAILS_SUBDIR,
    image_pack_relative_path,
    media_image_root_path,
)

name = "20261008_01_detach_media_point_and_video_cover_images"


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _read_source_bytes(origin: str) -> bytes:
    image_root = media_image_root_path()
    normalized = PurePosixPath(origin.replace("\\", "/"))
    pack_relative = image_pack_relative_path(origin)
    if pack_relative is not None:
        pack_path = image_root / pack_relative
        if pack_path.is_file():
            try:
                with zipfile.ZipFile(pack_path) as archive:
                    return archive.read(normalized.name)
            except (KeyError, zipfile.BadZipFile):
                pass
    return (image_root / normalized).read_bytes()


def _write_asset_bytes(origin: str, data: bytes) -> None:
    target_path = media_image_root_path() / PurePosixPath(origin)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, target_path)
    except Exception:
        _unlink_quietly(tmp_path)
        raise


def _remove_pack_entry(pack_path: Path, entry_name: str) -> None:
    with zipfile.ZipFile(pack_path) as archive:
        remaining = [
            (name, archive.read(name))
            for name in archive.namelist()
            if name != entry_name
        ]
    if not remaining:
        _unlink_quietly(pack_path)
        return
    tmp_path = pack_path.with_name(f"{pack_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        write_pack(tmp_path, remaining)
        os.replace(tmp_path, pack_path)
    except Exception:
        _unlink_quietly(tmp_path)
        raise


def _remove_old_image_file(origin: str) -> None:
    image_root = media_image_root_path()
    loose_path = image_root / PurePosixPath(origin)
    pack_relative = image_pack_relative_path(origin)
    if pack_relative is not None:
        pack_path = image_root / pack_relative
        if pack_path.is_file():
            _remove_pack_entry(pack_path, PurePosixPath(origin).name)
    _unlink_quietly(loose_path)


def _unused_image_condition(tables: set[str]) -> str:
    # 与 ImageCleanupService.image_record_is_still_used 的引用面一致；缺表时跳过对应检查。
    checks: list[str] = []
    if "movie" in tables:
        checks.append(
            "SELECT 1 FROM movie AS m "
            "WHERE m.cover_image_id = image.id OR m.thin_cover_image_id = image.id"
        )
    if "actor" in tables:
        checks.append(
            "SELECT 1 FROM actor AS a "
            "WHERE a.profile_image_id = image.id OR a.profile_image_override_id = image.id"
        )
    if "movie_plot_image" in tables:
        checks.append("SELECT 1 FROM movie_plot_image AS p WHERE p.image_id = image.id")
    if "media_thumbnail" in tables:
        checks.append("SELECT 1 FROM media_thumbnail AS t WHERE t.image_id = image.id")
    if "media_point" in tables:
        checks.append("SELECT 1 FROM media_point AS pt WHERE pt.image_id = image.id")
    if "video_item" in tables:
        checks.append("SELECT 1 FROM video_item AS v WHERE v.cover_image_id = image.id")
    if not checks:
        return "FALSE"
    return " OR ".join(f"EXISTS ({check})" for check in checks)


def _is_thumbnail_derived_origin(origin: str | None) -> bool:
    normalized = PurePosixPath((origin or "").strip().replace("\\", "/"))
    return normalized.parent.name == MEDIA_THUMBNAILS_SUBDIR


def _insert_image_row(database, origin: str) -> int:
    # 与 utc_now_for_db 同语义的 naive UTC 时间戳。
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = database.execute_sql(
        "INSERT INTO image (origin, created_at, updated_at) VALUES (%s, %s, %s) "
        "RETURNING id",
        (origin, now, now),
    ).fetchone()
    return int(row[0])


def _delete_old_image_if_unused(
    database, not_used_condition: str, image_id: int, origin: str
) -> None:
    cursor = database.execute_sql(
        f"DELETE FROM image WHERE image.id = %s AND NOT ({not_used_condition})",
        (image_id,),
    )
    if cursor.rowcount:
        _remove_old_image_file(origin)


def migrate(database) -> None:
    tables = set(database.get_tables())
    if "image" not in tables:
        return

    point_rows: list[tuple[int, str]] = []
    if "media_point" in tables:
        point_rows = database.execute_sql(
            "SELECT p.image_id, i.origin FROM media_point AS p "
            "JOIN image AS i ON i.id = p.image_id ORDER BY p.image_id"
        ).fetchall()
    cover_rows: list[tuple[int, int, str]] = []
    if "video_item" in tables:
        cover_rows = database.execute_sql(
            "SELECT v.id, v.cover_image_id, i.origin FROM video_item AS v "
            "JOIN image AS i ON i.id = v.cover_image_id ORDER BY v.id"
        ).fetchall()

    not_used_condition = _unused_image_condition(tables)
    detached_points = detached_covers = failed = 0
    # 迁移自身保证处于事务中：runner 会包一层 atomic，直接调用（如测试）时这里补齐。
    with database.atomic():
        # 时刻：同一张来源图可能被多个时刻共享，按 image_id 归并，一次拷贝复用到全部时刻。
        point_groups: dict[int, list[str]] = {}
        for image_id, origin in point_rows:
            if _is_thumbnail_derived_origin(origin):
                point_groups.setdefault(int(image_id), []).append(origin or "")
        for image_id, origins in point_groups.items():
            source_origin = origins[0]
            new_origin: str | None = None
            try:
                with database.savepoint():
                    new_origin = f"{MEDIA_POINTS_SUBDIR}/{uuid.uuid4().hex}.webp"
                    _write_asset_bytes(new_origin, _read_source_bytes(source_origin))
                    new_image_id = _insert_image_row(database, new_origin)
                    database.execute_sql(
                        "UPDATE media_point SET image_id = %s WHERE image_id = %s",
                        (new_image_id, image_id),
                    )
                    _delete_old_image_if_unused(
                        database, not_used_condition, image_id, source_origin
                    )
                    detached_points += len(origins)
            except Exception as exc:
                failed += 1
                if new_origin is not None:
                    _unlink_quietly(media_image_root_path() / PurePosixPath(new_origin))
                logger.warning(
                    "Detach media point image failed image_id={} origin={} detail={}",
                    image_id,
                    source_origin,
                    exc,
                )

        # 视频封面：每个视频独立拷贝；即使旧图同时被时刻引用，也各自拥有副本。
        for video_id, image_id, origin in cover_rows:
            if not _is_thumbnail_derived_origin(origin):
                continue
            new_origin = None
            try:
                with database.savepoint():
                    new_origin = f"videos/{int(video_id)}/cover/{uuid.uuid4().hex}.webp"
                    _write_asset_bytes(new_origin, _read_source_bytes(origin))
                    new_image_id = _insert_image_row(database, new_origin)
                    database.execute_sql(
                        "UPDATE video_item SET cover_image_id = %s WHERE id = %s",
                        (new_image_id, video_id),
                    )
                    _delete_old_image_if_unused(
                        database, not_used_condition, image_id, origin
                    )
                    detached_covers += 1
            except Exception as exc:
                failed += 1
                if new_origin is not None:
                    _unlink_quietly(media_image_root_path() / PurePosixPath(new_origin))
                logger.warning(
                    "Detach video cover image failed video_id={} origin={} detail={}",
                    video_id,
                    origin,
                    exc,
                )

    logger.info(
        "Detach user asset images migration finished points={} covers={} failed={}",
        detached_points,
        detached_covers,
        failed,
    )
