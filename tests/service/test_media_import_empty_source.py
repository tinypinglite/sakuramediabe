import json

from src.model import DownloadClient, DownloadTask, MediaLibrary
from src.service.transfers.media_import_service import MediaImportService


def test_download_import_with_no_media_files_is_failed(test_db, tmp_path):
    source = tmp_path / "empty-download"
    source.mkdir()
    library = MediaLibrary.create(
        name="Local",
        backend="local",
        backend_config={"root_path": str(tmp_path / "library")},
    )
    client = DownloadClient.create(
        name="qBittorrent",
        kind="qbittorrent",
        base_url="http://qbittorrent:8080",
        username="admin",
        password="password",
        client_save_path="/downloads",
        local_root_path=str(tmp_path),
        media_library=library,
    )
    task = DownloadTask.create(
        client=client,
        movie="TEST-001",
        name="TEST-001",
        info_hash="test-hash",
        save_path=str(source),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )
    service = MediaImportService(
        provider=object(),
        catalog_import_service=object(),
        media_metadata_probe_service=object(),
    )

    job = service.import_from_source(
        str(source),
        library.id,
        download_task_id=task.id,
    )

    task = DownloadTask.get_by_id(task.id)
    failures = json.loads(job.failed_files)
    assert job.state == "failed"
    assert job.imported_count == 0
    assert job.skipped_count == 0
    assert job.failed_count == 1
    assert failures == [
        {
            "path": str(source.resolve()),
            "reason": "no_media_files_found",
            "kind": "job",
            "detail": "下载目录中没有扫描到可导入的视频",
        }
    ]
    assert task.import_status == "failed"
