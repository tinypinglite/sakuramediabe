"""字幕统一迁移 service 的真实 DB 行为测试。

字幕搬运是不可逆的文件操作，硬链接 + file_path 单条 UPDATE 的崩溃恢复语义是设计核心。
用 clean_db 建真实 schema，tmp_path 承载图片根与旧字幕根，直接调 service.run()。
"""

from __future__ import annotations

from pathlib import Path

from src.common.media_paths import movie_asset_shard
from src.config.config import settings
from src.model import Movie, Subtitle
from src.service.catalog.movie_subtitle_unify_migration_service import (
    MovieSubtitleUnifyMigrationService,
)
from tests.conftest import TEST_MODELS


def _point_roots_at_tmp(monkeypatch, tmp_path) -> tuple[Path, Path]:
    image_root = tmp_path / "assets"
    legacy_subtitle_root = tmp_path / "subtitles"
    image_root.mkdir()
    legacy_subtitle_root.mkdir()
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    monkeypatch.setattr(settings.media, "subtitle_root_path", str(legacy_subtitle_root))
    return image_root, legacy_subtitle_root


def _insert_movie(movie_number: str, javdb_id: str) -> int:
    return Movie.create(javdb_id=javdb_id, movie_number=movie_number, title="title").id


def _subtitle_target_dir(image_root: Path, movie_number: str) -> Path:
    return image_root / "movies" / movie_asset_shard(movie_number) / movie_number / "subtitles"


def test_unifies_local_sidecar_and_legacy_cloud_subtitles(clean_db, monkeypatch, tmp_path):
    """媒体库 sidecar 与旧字幕根的 .srt 都搬进 movies/<shard>/<番号>/subtitles/<番号>-<N>.srt。"""
    image_root, legacy_subtitle_root = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    local_movie_id = _insert_movie("SONE-210", "jdb-1")
    cloud_movie_id = _insert_movie("ABP-123", "jdb-2")

    # 本地媒体：字幕作为 sidecar 躺在媒体库版本目录里。
    version_dir = tmp_path / "library" / "jav" / "SONE-210" / "1758000000000"
    version_dir.mkdir(parents=True)
    sidecar = version_dir / "SONE-210.srt"
    sidecar.write_text("local sub", encoding="utf-8")
    Subtitle.create(movie=local_movie_id, file_path=str(sidecar))

    # 115 云盘媒体：字幕在旧字幕根下（原名带 fid 编码）。
    legacy_dir = legacy_subtitle_root / "ABP-123"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / "ABP-123＿CD1＿movie.srt"
    legacy_file.write_text("cloud sub", encoding="utf-8")
    Subtitle.create(movie=cloud_movie_id, file_path=str(legacy_file))

    MovieSubtitleUnifyMigrationService.run()

    # 每部影片的字幕都改名为 <番号>-1.srt（本机第一次分配，序号从 1 起）。
    local_target = _subtitle_target_dir(image_root, "SONE-210") / "SONE-210-1.srt"
    cloud_target = _subtitle_target_dir(image_root, "ABP-123") / "ABP-123-1.srt"
    assert local_target.read_text(encoding="utf-8") == "local sub"
    assert cloud_target.read_text(encoding="utf-8") == "cloud sub"
    # 旧位置的文件被删掉，媒体库里不再有 .srt。
    assert not sidecar.exists()
    assert not legacy_file.exists()

    file_paths = {row.file_path for row in Subtitle.select()}
    assert file_paths == {str(local_target), str(cloud_target)}


def test_multiple_sidecars_in_same_version_dir_get_distinct_sequences(
    clean_db, monkeypatch, tmp_path,
):
    """同一版本目录里两份 srt（如 whisperjav 的 chinese/plain）拿到不同序号，不会互相覆盖。"""
    image_root, _ = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    movie_id = _insert_movie("FNS-164", "jdb-fns")
    version_dir = tmp_path / "library" / "jav" / "FNS-164" / "1773477363064"
    version_dir.mkdir(parents=True)
    sidecar_chs = version_dir / "FNS-164.ja.whisperjav.chinese.srt"
    sidecar_ja = version_dir / "FNS-164.ja.whisperjav.srt"
    sidecar_chs.write_text("chs", encoding="utf-8")
    sidecar_ja.write_text("ja", encoding="utf-8")
    # DB 里按创建顺序两条独立字幕行。
    row_a = Subtitle.create(movie=movie_id, file_path=str(sidecar_chs))
    row_b = Subtitle.create(movie=movie_id, file_path=str(sidecar_ja))

    stats = MovieSubtitleUnifyMigrationService.run()

    target_dir = _subtitle_target_dir(image_root, "FNS-164")
    # 两条字幕拿到相邻序号；具体分配顺序按 Subtitle.id 升序。
    assert (target_dir / "FNS-164-1.srt").read_text(encoding="utf-8") == "chs"
    assert (target_dir / "FNS-164-2.srt").read_text(encoding="utf-8") == "ja"
    assert not sidecar_chs.exists()
    assert not sidecar_ja.exists()
    assert stats.subtitles_migrated == 2
    assert stats.subtitles_scanned == 2
    assert Subtitle.get_by_id(row_a.id).file_path == str(target_dir / "FNS-164-1.srt")
    assert Subtitle.get_by_id(row_b.id).file_path == str(target_dir / "FNS-164-2.srt")


def test_legacy_flat_name_in_target_dir_gets_renamed_to_new_scheme(
    clean_db, monkeypatch, tmp_path,
):
    """老一轮遗产：字幕已在 subtitles/ 但叫 <版本>.srt，本轮统一改名为 <番号>-<N>.srt。"""
    image_root, _ = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    movie_id = _insert_movie("SSNI-888", "jdb-ssni")
    target_dir = _subtitle_target_dir(image_root, "SSNI-888")
    target_dir.mkdir(parents=True)
    legacy_named = target_dir / "1773406557182.srt"       # 上一轮迁移产物
    legacy_named.write_text("only sub", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie_id, file_path=str(legacy_named))

    stats = MovieSubtitleUnifyMigrationService.run()

    new_target = target_dir / "SSNI-888-1.srt"
    assert new_target.read_text(encoding="utf-8") == "only sub"
    assert not legacy_named.exists()
    assert Subtitle.get_by_id(subtitle.id).file_path == str(new_target)
    assert stats.subtitles_migrated == 1


def test_already_correct_named_row_is_skipped_and_new_row_gets_next_seq(
    clean_db, monkeypatch, tmp_path,
):
    """已经符合 <番号>-<N>.srt 的行 fast-path skip，同 movie 的新行从 max+1 起分配。"""
    image_root, _ = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    movie_id = _insert_movie("MIDE-500", "jdb-mide")
    target_dir = _subtitle_target_dir(image_root, "MIDE-500")
    target_dir.mkdir(parents=True)

    # 已在正确目录 & 正确命名的行：skip，seq=3 登入 reserved。
    existing_path = target_dir / "MIDE-500-3.srt"
    existing_path.write_text("existing", encoding="utf-8")
    row_existing = Subtitle.create(movie=movie_id, file_path=str(existing_path))

    # 待迁移的 sidecar：期望分配到 seq=4（max(3, 已 reserved) + 1）。
    version_dir = tmp_path / "library" / "jav" / "MIDE-500" / "1758000000123"
    version_dir.mkdir(parents=True)
    sidecar = version_dir / "MIDE-500.srt"
    sidecar.write_text("fresh", encoding="utf-8")
    row_pending = Subtitle.create(movie=movie_id, file_path=str(sidecar))

    stats = MovieSubtitleUnifyMigrationService.run()

    assert stats.subtitles_skipped_ok == 1
    assert stats.subtitles_migrated == 1
    assert Subtitle.get_by_id(row_existing.id).file_path == str(existing_path)
    new_target = target_dir / "MIDE-500-4.srt"
    assert Subtitle.get_by_id(row_pending.id).file_path == str(new_target)
    assert new_target.read_text(encoding="utf-8") == "fresh"


def test_crash_after_transfer_but_before_commit_still_moves_forward(
    clean_db, monkeypatch, tmp_path,
):
    """崩溃语义：STEP 1 已完成（新文件在位）但 STEP 2 未提交时崩掉，重跑给该行分配新 seq。

    seq-based 命名下无法精确"认领"上次分配的位置（file_path 还指向老 sidecar，看不出上次
    要落到哪个 seq），因此重跑会给该行拿一个新的 seq、把老 sidecar 再搬一次；上次已就位的
    文件成为无 owner 孤儿，由 sync_movie_subtitles 后续收敛。此测试锁定这个行为，避免以后
    误改回"精确认领"的复杂路径。
    """
    image_root, _ = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    movie_id = _insert_movie("MIDE-999", "jdb-3")
    version_dir = tmp_path / "library" / "jav" / "MIDE-999" / "1758000000001"
    version_dir.mkdir(parents=True)
    sidecar = version_dir / "MIDE-999.srt"
    sidecar.write_text("sub", encoding="utf-8")
    Subtitle.create(movie=movie_id, file_path=str(sidecar))

    # 模拟上一轮 STEP 1 已完成：<subtitles>/MIDE-999-1.srt 已在位，但 DB 还指向 sidecar。
    target_dir = _subtitle_target_dir(image_root, "MIDE-999")
    target_dir.mkdir(parents=True)
    orphan = target_dir / "MIDE-999-1.srt"
    orphan.write_text("sub", encoding="utf-8")

    stats = MovieSubtitleUnifyMigrationService.run()

    # 分配器看到 -1 已占位（磁盘扫描），本 row 拿到 -2；老 sidecar 复制到 -2，DB 更新到 -2，
    # sidecar 被 unlink；-1 成为孤儿。
    assert stats.subtitles_migrated == 1
    assert not sidecar.exists()
    assert orphan.exists()          # 上一轮遗留的文件保留，等 sync 收敛
    updated = Subtitle.get_by_id(1).file_path
    assert updated == str(target_dir / "MIDE-999-2.srt")
