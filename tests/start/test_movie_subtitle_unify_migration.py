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


def test_unifies_local_sidecar_and_legacy_cloud_subtitles(clean_db, monkeypatch, tmp_path):
    """媒体库 sidecar 与旧字幕根的 .srt 都搬进 movies/<shard>/<番号>/subtitles/。"""
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

    # 115 云盘媒体：字幕在旧字幕根下。
    legacy_dir = legacy_subtitle_root / "ABP-123"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / "ABP-123＿CD1＿movie.srt"
    legacy_file.write_text("cloud sub", encoding="utf-8")
    Subtitle.create(movie=cloud_movie_id, file_path=str(legacy_file))

    MovieSubtitleUnifyMigrationService.run()

    local_target = (
        image_root / "movies" / movie_asset_shard("SONE-210") / "SONE-210"
        / "subtitles" / "1758000000000.srt"
    )
    cloud_target = (
        image_root / "movies" / movie_asset_shard("ABP-123") / "ABP-123"
        / "subtitles" / "ABP-123＿CD1＿movie.srt"
    )
    # 本地 sidecar 用版本目录名命名，避免同番号多次导入互相覆盖；云盘侧保留已去重的原名。
    assert local_target.read_text(encoding="utf-8") == "local sub"
    assert cloud_target.read_text(encoding="utf-8") == "cloud sub"
    # 旧位置的文件被删掉，媒体库里不再有 .srt。
    assert not sidecar.exists()
    assert not legacy_file.exists()

    file_paths = {row.file_path for row in Subtitle.select()}
    assert file_paths == {str(local_target), str(cloud_target)}


def test_unify_recovers_when_db_rolled_back(clean_db, monkeypatch, tmp_path):
    """崩溃语义：文件已就位但 file_path 未提交，重跑只补 STEP 2。"""
    image_root, _ = _point_roots_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    movie_id = _insert_movie("MIDE-999", "jdb-3")
    version_dir = tmp_path / "library" / "jav" / "MIDE-999" / "1758000000001"
    version_dir.mkdir(parents=True)
    sidecar = version_dir / "MIDE-999.srt"
    sidecar.write_text("sub", encoding="utf-8")
    Subtitle.create(movie=movie_id, file_path=str(sidecar))

    # 新路径已就位、旧文件已删，只差 DB 提交。
    target_dir = image_root / "movies" / movie_asset_shard("MIDE-999") / "MIDE-999" / "subtitles"
    target_dir.mkdir(parents=True)
    (target_dir / "1758000000001.srt").write_text("sub", encoding="utf-8")
    sidecar.unlink()

    stats = MovieSubtitleUnifyMigrationService.run()

    assert stats.subtitles_recovered == 1
    assert stats.subtitles_migrated == 0
    assert Subtitle.get_by_id(1).file_path == str(target_dir / "1758000000001.srt")
