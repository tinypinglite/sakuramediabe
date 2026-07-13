import pytest

from src.config.config import settings
from src.model import DownloadClient, MediaLibrary
from src.service.transfers.download_small_file_cleanup_service import (
    NEED_DELETE_MARKER,
    DownloadSmallFileCleanupService,
)

MB = 1024 * 1024


@pytest.fixture()
def cleanup_tables(test_db):
    models = [MediaLibrary, DownloadClient]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_client(*, name: str, local_root_path: str) -> DownloadClient:
    library = MediaLibrary.create(name=f"lib-{name}", root_path=f"/library/{name}")
    return DownloadClient.create(
        name=name,
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path=f"/downloads/{name}",
        local_root_path=local_root_path,
        media_library=library,
    )


def _fake_qb_cls(registry: dict, calls: dict):
    """构造一个按 client.id 取数据、并记录写操作的假 qBittorrent 客户端类。"""

    class FakeQB:
        def __init__(self, client_id):
            self.client_id = client_id

        @classmethod
        def from_download_client(cls, client):
            return cls(client.id)

        def list_torrents(self, *, client_id=None):
            spec = registry[self.client_id]
            if spec.get("raise_list"):
                raise RuntimeError("list boom")
            return spec.get("torrents", [])

        def list_torrent_files(self, info_hash):
            return registry[self.client_id]["files"][info_hash]

        def set_files_not_download(self, info_hash, indexes):
            calls["set"].append((info_hash, list(indexes)))

        def rename_torrent_file(self, info_hash, index, new_name):
            calls["rename"].append((info_hash, index, new_name))

    return FakeQB


@pytest.fixture()
def _threshold_256mb(monkeypatch):
    monkeypatch.setattr(settings.downloads, "small_file_cleanup_threshold_mb", 256)


def test_cleans_small_files_and_skips_large_and_already_deselected(
    cleanup_tables, tmp_path, _threshold_256mb
):
    client = _create_client(name="a", local_root_path=str(tmp_path))
    calls = {"set": [], "rename": []}
    registry = {
        client.id: {
            "torrents": [{"info_hash": "hash-a", "progress": 0.5}],
            "files": {
                "hash-a": [
                    {"index": 0, "name": "small.mkv", "size": 100 * MB, "priority": 1},
                    {"index": 1, "name": "movie.mkv", "size": 700 * MB, "priority": 1},
                    # 已是不下载（priority=0）的小文件应跳过，保证幂等
                    {"index": 2, "name": "tiny.txt", "size": 50 * MB, "priority": 0},
                ]
            },
        }
    }
    service = DownloadSmallFileCleanupService(
        qbittorrent_client_cls=_fake_qb_cls(registry, calls)
    )

    stats = service.cleanup_small_files()

    assert calls["set"] == [("hash-a", [0])]
    assert len(calls["rename"]) == 1
    assert calls["rename"][0][0] == "hash-a"
    assert calls["rename"][0][1] == 0
    assert calls["rename"][0][2].startswith(f"{NEED_DELETE_MARKER}_")
    assert stats["scanned_torrents"] == 1
    assert stats["deselected_files"] == 1
    assert stats["failed_count"] == 0


def test_skips_completed_torrents(cleanup_tables, tmp_path, _threshold_256mb):
    client = _create_client(name="a", local_root_path=str(tmp_path))
    calls = {"set": [], "rename": []}
    registry = {
        client.id: {
            "torrents": [{"info_hash": "hash-done", "progress": 1.0}],
            "files": {
                "hash-done": [
                    {"index": 0, "name": "small.mkv", "size": 10 * MB, "priority": 1},
                ]
            },
        }
    }
    service = DownloadSmallFileCleanupService(
        qbittorrent_client_cls=_fake_qb_cls(registry, calls)
    )

    stats = service.cleanup_small_files()

    assert calls["set"] == []
    assert calls["rename"] == []
    assert stats["scanned_torrents"] == 0
    assert stats["deselected_files"] == 0


def test_physical_delete_removes_marked_files(cleanup_tables, tmp_path, _threshold_256mb):
    # 存在下载中种子时，暂存目录里含 need_delete 标记的文件应被物理删除，其它文件保留
    marked = tmp_path / "sub" / f"{NEED_DELETE_MARKER}_abc.mkv"
    marked.parent.mkdir(parents=True)
    marked.write_text("x")
    keep = tmp_path / "movie.mkv"
    keep.write_text("y")

    client = _create_client(name="a", local_root_path=str(tmp_path))
    registry = {
        client.id: {
            "torrents": [{"info_hash": "hash-a", "progress": 0.5}],
            "files": {
                "hash-a": [
                    {"index": 0, "name": "movie.mkv", "size": 700 * MB, "priority": 1},
                ]
            },
        }
    }
    service = DownloadSmallFileCleanupService(
        qbittorrent_client_cls=_fake_qb_cls(registry, {"set": [], "rename": []})
    )

    stats = service.cleanup_small_files()

    assert not marked.exists()
    assert keep.exists()
    assert stats["deleted_files"] == 1


def test_skips_directory_walk_when_no_incomplete_torrents(
    cleanup_tables, tmp_path, _threshold_256mb
):
    # 没有下载中种子时不遍历下载目录（避免高频空扫唤醒硬盘）,历史遗留标记文件保持原样
    leftover = tmp_path / f"{NEED_DELETE_MARKER}_old.mkv"
    leftover.write_text("x")

    client = _create_client(name="a", local_root_path=str(tmp_path))
    registry = {
        client.id: {
            # 一个已完成种子 + 空列表都属于"无下载中种子"
            "torrents": [{"info_hash": "hash-done", "progress": 1.0}],
        }
    }
    service = DownloadSmallFileCleanupService(
        qbittorrent_client_cls=_fake_qb_cls(registry, {"set": [], "rename": []})
    )

    stats = service.cleanup_small_files()

    assert leftover.exists()
    assert stats["scanned_torrents"] == 0
    assert stats["deleted_files"] == 0


def test_failed_client_is_counted_and_does_not_block_others(
    cleanup_tables, tmp_path, _threshold_256mb
):
    client_a = _create_client(name="a", local_root_path=str(tmp_path / "a"))
    client_b = _create_client(name="b", local_root_path=str(tmp_path / "b"))
    (tmp_path / "b").mkdir()
    calls = {"set": [], "rename": []}
    registry = {
        client_a.id: {"raise_list": True},
        client_b.id: {
            "torrents": [{"info_hash": "hash-b", "progress": 0.2}],
            "files": {
                "hash-b": [
                    {"index": 0, "name": "small.mkv", "size": 100 * MB, "priority": 1},
                ]
            },
        },
    }
    service = DownloadSmallFileCleanupService(
        qbittorrent_client_cls=_fake_qb_cls(registry, calls)
    )

    stats = service.cleanup_small_files()

    # client_a 抛错被计入 failed_count，但 client_b 仍正常处理
    assert stats["failed_count"] == 1
    assert stats["total_clients"] == 2
    assert calls["set"] == [("hash-b", [0])]
    assert stats["deselected_files"] == 1
