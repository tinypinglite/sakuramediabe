"""影片资产目录分片迁移 service 的真实 DB 行为测试。

分片是一次性、不可逆的 30w 级文件系统操作，崩溃恢复语义是设计核心，值得护栏守护。
用 clean_db 建真实 schema，用 tmp_path 承载图片根，直接调 service.run()（不走 CLI mock）。
"""

from __future__ import annotations

from pathlib import Path

from src.common.media_paths import movie_asset_shard
from src.config.config import settings
from src.model import Image
from src.service.catalog.movie_asset_shard_migration_service import (
    MovieAssetShardMigrationService,
)
from tests.conftest import TEST_MODELS


def _point_image_root_at_tmp(monkeypatch, tmp_path) -> Path:
    # service 运行期读 settings 解析图片根，monkeypatch 即可重定向到临时目录。
    image_root = tmp_path / "assets"
    image_root.mkdir()
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    return image_root


def _write_legacy_movie_assets(image_root: Path, movie_number: str) -> Path:
    """按老平铺布局造出 movies/<番号>/ 下的封面、剧照和一张缩略图。"""
    movie_dir = image_root / "movies" / movie_number
    (movie_dir / "media" / "fp-1" / "thumbnails").mkdir(parents=True)
    (movie_dir / "cover.jpg").write_bytes(b"cover")
    (movie_dir / "plot-0.jpg").write_bytes(b"plot")
    (movie_dir / "media" / "fp-1" / "thumbnails" / "10.webp").write_bytes(b"thumb")
    return movie_dir


def test_shards_movie_asset_dirs_and_rewrites_origins(clean_db, monkeypatch, tmp_path):
    """movies/<番号>/ 整目录搬进 sha1 分片，image.origin 同步重写；actors/videos 不动。"""
    image_root = _point_image_root_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    _write_legacy_movie_assets(image_root, "SONE-210")
    for origin in (
        "movies/SONE-210/cover.jpg",
        "movies/SONE-210/plot-0.jpg",
        "movies/SONE-210/media/fp-1/thumbnails/10.webp",
    ):
        Image.create(origin=origin, small=origin, medium=origin, large=origin)
    # actors/ 与 videos/ 不属于分片范围，必须原样保留。
    Image.create(origin="actors/EM44.jpg", small="actors/EM44.jpg",
                 medium="actors/EM44.jpg", large="actors/EM44.jpg")

    MovieAssetShardMigrationService.run()

    shard = movie_asset_shard("SONE-210")
    sharded_dir = image_root / "movies" / shard / "SONE-210"
    # 文件确实搬到了分片目录下，整棵子树（含 media/thumbnails）一起过去。
    assert (sharded_dir / "cover.jpg").read_bytes() == b"cover"
    assert (sharded_dir / "media" / "fp-1" / "thumbnails" / "10.webp").read_bytes() == b"thumb"
    assert not (image_root / "movies" / "SONE-210").exists()
    # 顶层只剩 256 个分片目录。
    assert len(list((image_root / "movies").iterdir())) == 256

    origins = {image.origin for image in Image.select()}
    assert origins == {
        f"movies/{shard}/SONE-210/cover.jpg",
        f"movies/{shard}/SONE-210/plot-0.jpg",
        f"movies/{shard}/SONE-210/media/fp-1/thumbnails/10.webp",
        "actors/EM44.jpg",
    }
    # small/medium/large 与 origin 保持一致。
    cover = Image.get(Image.origin == f"movies/{shard}/SONE-210/cover.jpg")
    assert cover.small == cover.medium == cover.large == cover.origin


def test_shard_is_idempotent_on_rerun(clean_db, monkeypatch, tmp_path):
    """重跑幂等：已分片的目录与 origin 都不再被改动。"""
    image_root = _point_image_root_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    _write_legacy_movie_assets(image_root, "ABP-123")
    Image.create(origin="movies/ABP-123/cover.jpg", small="movies/ABP-123/cover.jpg",
                 medium="movies/ABP-123/cover.jpg", large="movies/ABP-123/cover.jpg")

    first = MovieAssetShardMigrationService.run()
    assert first.directories_sharded == 1
    assert first.images_rewritten == 1

    second = MovieAssetShardMigrationService.run()
    assert second.directories_sharded == 0
    assert second.images_rewritten == 0
    shard = movie_asset_shard("ABP-123")
    assert Image.get_by_id(1).origin == f"movies/{shard}/ABP-123/cover.jpg"


def test_shard_recovers_when_db_rolled_back(clean_db, monkeypatch, tmp_path):
    """崩溃语义：文件已归片但 DB 未提交（模拟 rename 后掉电），重跑只补 origin 重写。"""
    image_root = _point_image_root_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    # 手工把目录摆成"已归片"，DB 仍是老 origin —— 正是 Phase 1 跑完、掉电后的状态。
    shard = movie_asset_shard("MIDE-999")
    sharded_dir = image_root / "movies" / shard / "MIDE-999"
    sharded_dir.mkdir(parents=True)
    (sharded_dir / "cover.jpg").write_bytes(b"cover")
    Image.create(origin="movies/MIDE-999/cover.jpg", small="movies/MIDE-999/cover.jpg",
                 medium="movies/MIDE-999/cover.jpg", large="movies/MIDE-999/cover.jpg")

    stats = MovieAssetShardMigrationService.run()

    assert stats.directories_sharded == 0
    assert stats.images_rewritten == 1
    assert Image.get_by_id(1).origin == f"movies/{shard}/MIDE-999/cover.jpg"


def test_shard_terminates_on_merge_conflict_without_infinite_loop(
    clean_db, monkeypatch, tmp_path
):
    """merge 冲突残留必须退出循环：源目录同名文件与分片目录同名文件互撞时，
    源目录会留在顶层交人工处理，但迁移本身要能收敛退出、不能反复扫入同一个残留目录。
    """
    image_root = _point_image_root_at_tmp(monkeypatch, tmp_path)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    # 老布局残留 movies/SONE-210/cover.jpg —— 老用户没跑迁移前重导过一次
    legacy_dir = image_root / "movies" / "SONE-210"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "cover.jpg").write_bytes(b"legacy cover")
    (legacy_dir / "extra.jpg").write_bytes(b"legacy extra")

    # 分片位置已经有一份 cover.jpg（同一部影片重导时新写入的）
    shard = movie_asset_shard("SONE-210")
    sharded_dir = image_root / "movies" / shard / "SONE-210"
    sharded_dir.mkdir(parents=True)
    (sharded_dir / "cover.jpg").write_bytes(b"sharded cover")

    Image.create(
        origin=f"movies/{shard}/SONE-210/cover.jpg",
        small=f"movies/{shard}/SONE-210/cover.jpg",
        medium=f"movies/{shard}/SONE-210/cover.jpg",
        large=f"movies/{shard}/SONE-210/cover.jpg",
    )

    stats = MovieAssetShardMigrationService.run()

    # 关键护栏：源目录只被处理一次，不会因 rmdir 失败反复重扫。
    assert stats.directories_scanned == 1
    assert stats.directories_conflict_skipped == 1
    assert stats.directories_sharded == 0
    assert stats.directories_merged == 0
    # 分片侧的 cover.jpg 是权威，保持不变；未冲突的 extra.jpg 被并入分片目录。
    assert (sharded_dir / "cover.jpg").read_bytes() == b"sharded cover"
    assert (sharded_dir / "extra.jpg").read_bytes() == b"legacy extra"
    # 源目录留下 cover.jpg 残留，rmdir 失败，交人工处理。
    assert (legacy_dir / "cover.jpg").exists()
    assert not (legacy_dir / "extra.jpg").exists()
