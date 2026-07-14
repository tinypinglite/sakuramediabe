"""JavLayoutMigrationService 单元测试。

覆盖状态矩阵 8 行 + 幂等 + sidecar + inode 语义 + 跨 fs 降级 + STEP3 失败。
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from src.model import Image, Media, MediaLibrary, Movie, MovieSeries, Subtitle, VideoItem
from src.service.playback.jav_layout_migration_service import (
    JavLayoutMigrationService,
    MigrationStats,
)


@pytest.fixture()
def jav_layout_tables(test_db):
    models = [Image, MovieSeries, Movie, VideoItem, Subtitle, MediaLibrary, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _make_movie(movie_number: str) -> Movie:
    return Movie.create(movie_number=movie_number, javdb_id=movie_number, title=movie_number)


def _make_library(root: Path, name: str = "Main") -> MediaLibrary:
    root.mkdir(parents=True, exist_ok=True)
    return MediaLibrary.create(
        name=name,
        backend="local",
        backend_config={"root_path": str(root)},
    )


def _write_video_file(path: Path, content: bytes = b"video-bytes") -> None:
    """在指定路径写一个非空的假视频文件；用于验证硬链接语义。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _place_media(
    *,
    library: MediaLibrary,
    movie: Movie,
    style: str,
    write_file: bool,
    version_ms: int = 1730000000000,
) -> tuple[Media, Path, Path]:
    """按 style 布局把媒体行落到 old / new 目录并可选是否真写文件。

    返回 (media, old_path, new_path)。style ∈ {"old", "new"}，指 Media.path 存的前缀风格。
    """
    root = Path(library.backend_config["root_path"])
    old_path = root / movie.movie_number / str(version_ms) / f"{movie.movie_number}.mp4"
    new_path = root / "jav" / movie.movie_number / str(version_ms) / f"{movie.movie_number}.mp4"
    db_path = old_path if style == "old" else new_path
    if write_file:
        _write_video_file(db_path)
    media = Media.create(
        movie=movie,
        library=library,
        path=str(db_path),
        valid=True,
    )
    return media, old_path, new_path


# ---------------------------------------------------------------------------
# 状态矩阵 8 行
# ---------------------------------------------------------------------------


def test_state_A_full_migration(jav_layout_tables, tmp_path):
    """db=old + old_exists + !new_exists → 完整 STEP 1→2→3→4 → 终态 F。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-101")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_migrated == 1
    assert stats.media_recovered_to_f == 0
    assert new_path.exists()
    assert not old_path.exists()
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(new_path)


def test_recovery_step2_only_when_link_done_and_source_gone(jav_layout_tables, tmp_path):
    """db=old + !old_exists + new_exists → 只做 STEP 2，收敛到 F。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-102")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=False,
    )
    # 手工构造：新文件已在，但 DB 还指老、老已被删（模拟 STEP 1 成功后 STEP 3 完成但 STEP 2 前 crash）
    _write_video_file(new_path)

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_recovered_to_f == 1
    assert stats.media_migrated == 0
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(new_path)
    assert new_path.exists()


def test_recovery_step2_step3_when_link_done_source_alive(jav_layout_tables, tmp_path):
    """db=old + old_exists + new_exists → 中断态 (STEP 1 完成，STEP 2 前 crash) → 补 STEP 2+3。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-103")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )
    # 手工先建硬链接（模拟 STEP 1 已完成，DB 未 update）
    new_path.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    _os.link(old_path, new_path)

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_recovered_to_f == 1
    assert not old_path.exists()      # STEP 3 补上删源
    assert new_path.exists()
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(new_path)


def test_recovery_step3_when_db_new_source_alive(jav_layout_tables, tmp_path):
    """db=new + old_exists + new_exists → 中断态 (STEP 2 完成，STEP 3 前 crash) → 补 STEP 3。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-104")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="new", write_file=True,
    )
    # 手工留一份老残留（模拟硬链接后 STEP 2 完成但 STEP 3 未做）
    _write_video_file(old_path)

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_recovered_to_f == 1
    assert not old_path.exists()
    assert new_path.exists()
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(new_path)   # 保持 new


def test_fast_skip_ok_f(jav_layout_tables, tmp_path):
    """db=new + !old_exists + new_exists → F 终态 → fast-path skip，无 IO。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-105")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="new", write_file=True,
    )

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_skipped_ok_f == 1
    assert stats.media_migrated == 0
    assert stats.media_recovered_to_f == 0
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(new_path)


def test_data_lost_marks_invalid(jav_layout_tables, tmp_path):
    """db=old + !old_exists + !new_exists → 数据丢失 → 标 Media.valid=False，不 raise。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-106")
    media, _old, _new = _place_media(
        library=lib, movie=movie, style="old", write_file=False,
    )

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_data_lost == 1
    refreshed = Media.get_by_id(media.id)
    assert refreshed.valid is False


def test_rollback_when_db_new_but_new_missing_and_old_present(jav_layout_tables, tmp_path):
    """db=new + old_exists + !new_exists → 严重不一致 → 回滚 DB 到 old。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-107")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="new", write_file=False,
    )
    # DB 指新（style="new"），但新文件不写、老文件写
    _write_video_file(old_path)

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_rolled_back == 1
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(old_path)


def test_skip_unknown_layout(jav_layout_tables, tmp_path):
    """Media.path 前缀既不是 <root>/<num>/ 也不是 <root>/jav/<num>/ → 不动。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-108")
    # 手工挪过的路径：在完全无关的目录下
    weird_path = tmp_path / "some_other_root" / movie.movie_number / "file.mp4"
    _write_video_file(weird_path)
    media = Media.create(
        movie=movie, library=lib, path=str(weird_path), valid=True,
    )

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_skipped_unknown_layout == 1
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(weird_path)
    assert weird_path.exists()


def test_cloud115_library_is_not_scanned(jav_layout_tables):
    """JAV 本地布局迁移不读取或改动 cloud115 媒体库。"""
    cloud_library = MediaLibrary.create(
        name="Cloud115",
        backend="cloud115",
        backend_config={"root_cid": "cid-library"},
    )

    stats = JavLayoutMigrationService.run(library_id=cloud_library.id)

    assert stats.libraries_scanned == 0
    assert stats.media_migrated == 0
    assert stats.media_failed == 0


# ---------------------------------------------------------------------------
# 端到端幂等 + sidecar + inode + 降级 + STEP3 失败
# ---------------------------------------------------------------------------


def test_idempotent_two_runs_to_completion(jav_layout_tables, tmp_path):
    """第一次跑全部 A→F；第二次再跑一次，全部 fast-path skip。"""
    lib = _make_library(tmp_path / "lib")
    for i in range(3):
        movie = _make_movie(f"ABP-20{i}")
        _place_media(library=lib, movie=movie, style="old", write_file=True)

    stats1 = JavLayoutMigrationService.run(library_id=lib.id)
    assert stats1.media_migrated == 3
    assert stats1.media_skipped_ok_f == 0

    stats2 = JavLayoutMigrationService.run(library_id=lib.id)
    assert stats2.media_skipped_ok_f == 3
    assert stats2.media_migrated == 0


def test_partial_failure_does_not_block_others(jav_layout_tables, tmp_path):
    """一条 media 处于 rollback 分支，其他正常 A→F —— 不阻塞。"""
    lib = _make_library(tmp_path / "lib")
    # media_a 正常
    movie_a = _make_movie("ABP-301")
    _place_media(library=lib, movie=movie_a, style="old", write_file=True)
    # media_b 处于 rollback 状态（DB 新但新不存在、老在）
    movie_b = _make_movie("ABP-302")
    _, old_b, _new_b = _place_media(library=lib, movie=movie_b, style="new", write_file=False)
    _write_video_file(old_b)

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_migrated == 1
    assert stats.media_rolled_back == 1


def test_sidecar_migrated_together(jav_layout_tables, tmp_path):
    """<root>/<num>/<ver>/<num>.srt 存在 → 迁移后一同到新目录，源清空。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-401")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )
    # 在同版本目录放一个 sidecar 字幕文件
    old_srt = old_path.with_suffix(".srt")
    old_srt.write_text("sub content")

    stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_migrated == 1
    assert stats.sidecars_migrated == 1
    new_srt = new_path.with_suffix(".srt")
    assert new_srt.exists()
    assert new_srt.read_text() == "sub content"
    assert not old_srt.exists()


def test_prunes_empty_old_dirs(jav_layout_tables, tmp_path):
    """全部迁完 + sidecar 也走后，<root>/<num>/<ver>/ 和 <root>/<num>/ 自动清空。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-402")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )

    JavLayoutMigrationService.run(library_id=lib.id)

    # 老版本目录和番号目录都已清空
    assert not old_path.parent.exists()
    assert not old_path.parent.parent.exists()
    # 但 <root>/jav/ 还在
    assert (Path(lib.backend_config["root_path"]) / "jav").exists()


def test_hardlink_semantics_preserve_inode(jav_layout_tables, tmp_path):
    """迁移前后 stat().st_ino 一致 → 证明是硬链接不是复制。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-501")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )
    inode_before = old_path.stat().st_ino

    JavLayoutMigrationService.run(library_id=lib.id)

    assert new_path.exists()
    assert new_path.stat().st_ino == inode_before


def test_fallback_copy_across_filesystem(jav_layout_tables, tmp_path):
    """monkeypatch 让 os.link 抛 EXDEV → 降级 shutil.copy2；migrated 计数照常。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-601")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )
    original_content = old_path.read_bytes()

    with patch("src.service.playback.jav_layout_migration_service.os.link") as mock_link:
        mock_link.side_effect = OSError(errno.EXDEV, "cross-device link")
        stats = JavLayoutMigrationService.run(library_id=lib.id)

    assert stats.media_migrated == 1
    assert new_path.exists()
    assert new_path.read_bytes() == original_content
    # 跨 fs 走复制后 STEP 3 删源，两者内容一致但 inode 不同
    assert not old_path.exists()


def test_step3_unlink_failure_is_recorded_but_not_raised(jav_layout_tables, tmp_path):
    """monkeypatch 让 old_path.unlink 抛权限错 → 记 warning 但不 raise，statscounts 照常。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-701")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )

    original_unlink = Path.unlink

    def _selective_unlink(self, *args, **kwargs):
        # 只对主视频老路径的 unlink 抛错，其它调用照常放行（避免影响清理空目录等逻辑）
        if str(self) == str(old_path):
            raise OSError(errno.EACCES, "permission denied")
        return original_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", _selective_unlink):
        stats = JavLayoutMigrationService.run(library_id=lib.id)

    # STEP 1、2 完成，STEP 3 失败但没 raise，也没让 media_failed 上涨
    assert stats.media_migrated == 1
    assert stats.media_failed == 0
    assert new_path.exists()
    # 老残留还在（下次跑会补 STEP 3）
    assert old_path.exists()


# ---------------------------------------------------------------------------
# dry_run 语义
# ---------------------------------------------------------------------------


def test_dry_run_counts_but_does_not_write(jav_layout_tables, tmp_path):
    """dry_run 模式：stats 仍反映"会做什么"，但磁盘和 DB 都不变。"""
    lib = _make_library(tmp_path / "lib")
    movie = _make_movie("ABP-801")
    media, old_path, new_path = _place_media(
        library=lib, movie=movie, style="old", write_file=True,
    )

    stats = JavLayoutMigrationService.run(library_id=lib.id, dry_run=True)

    assert stats.media_migrated == 1   # stats 反映"如果真跑会做什么"
    # 但磁盘和 DB 都没动
    assert old_path.exists()
    assert not new_path.exists()
    refreshed = Media.get_by_id(media.id)
    assert refreshed.path == str(old_path)
