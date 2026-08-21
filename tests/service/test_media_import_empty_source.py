from src.model import MediaLibrary
from src.service.transfers.imports.import_service import MediaImportService


def test_download_import_with_no_media_files_is_failed(test_db, tmp_path):
    source = tmp_path / "empty-download"
    source.mkdir()
    library = MediaLibrary.create(
        name="Local",
        backend="local",
        backend_config={"root_path": str(tmp_path / "library")},
    )
    service = MediaImportService(
        provider=object(),
        catalog_import_service=object(),
        media_metadata_probe_service=object(),
    )
    result = service.import_from_source(
        str(source),
        library.id,
    )

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.failed_count == 1
