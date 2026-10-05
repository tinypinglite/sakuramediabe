"""清除命中"不裁切"番号规则影片的生成式薄封面。

FC2 / HEYZO / 欧美流媒体日期命名 / 素人纯数字编号的封面都是单张横图，历史上被
"实体盘合订封面书脊"启发式误裁出 thin-cover 切片；运行时已对这批番号跳过裁切，
存量数据由本迁移一次性清理：清关联、删 Image 行（确认无其它引用后），并把文件
从 assets.zip / 单文件布局中移除。规则与迁移逻辑一并冻结在此，不 import 运行时。
"""

from __future__ import annotations

import os
import re
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from loguru import logger

from src.common.image_store import write_pack
from src.common.media_paths import image_pack_relative_path, media_image_root_path

name = "20261006_01_remove_generated_thin_cover_for_skipped_movies"

# 与 movie_image_service.should_skip_thin_cover_crop 保持一致（大小写不敏感前缀匹配）。
_SKIP_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^FC2-", re.IGNORECASE),
    re.compile(r"^HEYZO-", re.IGNORECASE),
    re.compile(r"^[a-z][a-z0-9]*\.\d{2,4}\.\d{2}\.\d{2}", re.IGNORECASE),
    re.compile(r"^\d+[-_]\d+"),
)

_GENERATED_THIN_MARKER = "/thin-cover."


def _matches_skip_rules(movie_number: str) -> bool:
    number = (movie_number or "").strip()
    return any(pattern.match(number) for pattern in _SKIP_NUMBER_PATTERNS)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


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


def _remove_generated_thin_cover_file(origin: str) -> None:
    image_root = media_image_root_path()
    loose_path = image_root / PurePosixPath(origin)
    pack_relative = image_pack_relative_path(origin)
    if pack_relative is not None:
        pack_path = image_root / pack_relative
        if pack_path.is_file():
            _remove_pack_entry(pack_path, PurePosixPath(origin).name)
    _unlink_quietly(loose_path)


def _unused_image_condition(tables: set[str]) -> str:
    # 与 ImageCleanupService.image_record_is_still_used 的引用面一致；缺表时跳过对应检查，
    # 避免把仍被引用的 Image 行删掉（media_point 为 ON DELETE RESTRICT，必须保留检查）。
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


def migrate(database) -> None:
    tables = set(database.get_tables())
    if "movie" not in tables or "image" not in tables:
        return

    candidates = database.execute_sql(
        "SELECT m.id, m.movie_number, t.id, t.origin "
        "FROM movie AS m JOIN image AS t ON t.id = m.thin_cover_image_id "
        "WHERE t.origin LIKE %s",
        (f"%{_GENERATED_THIN_MARKER}%",),
    ).fetchall()
    not_used_condition = _unused_image_condition(tables)

    matched = 0
    cleared = 0
    failed = 0
    # 迁移自身保证处于事务中：runner 会包一层 atomic，直接调用（如测试）时这里补齐，
    # 逐部 savepoint 才能在两种入口下都可用。
    with database.atomic():
        for movie_id, movie_number, image_id, origin in candidates:
            if not _matches_skip_rules(movie_number):
                continue
            matched += 1
            try:
                # 单部影片用 savepoint 隔离：文件清理失败只回滚这一部，不阻塞整个迁移与容器启动。
                with database.savepoint():
                    database.execute_sql(
                        "UPDATE movie SET thin_cover_image_id = NULL WHERE id = %s",
                        (movie_id,),
                    )
                    database.execute_sql(
                        f"DELETE FROM image WHERE image.id = %s AND NOT ({not_used_condition})",
                        (image_id,),
                    )
                    _remove_generated_thin_cover_file(origin or "")
                cleared += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Remove generated thin cover failed movie_id={} movie_number={} origin={} detail={}",
                    movie_id,
                    movie_number,
                    origin,
                    exc,
                )

    logger.info(
        "Remove generated thin cover migration finished candidates={} matched={} cleared={} failed={}",
        len(candidates),
        matched,
        cleared,
        failed,
    )
