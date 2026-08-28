from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.transfers.imports.import_service import MediaImportService


def test_create_media_persists_provider_file_hash(test_db):
    library = MediaLibrary.create(
        name="hash-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="HASH-001", javdb_id="hash-1", title="hash")
    source = ImportFile(
        source_ref={"path": "source.mp4"},
        name="HASH-001.mp4",
        relative_path="HASH-001.mp4",
        size_bytes=123,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"path": "media.mp4"},
        receipt={"receipt": "hash"},
        size_bytes=123,
        duration_seconds=60,
        video_info=None,
    )
    file_hash = "media-file-hash-v1:" + "a" * 40

    class Storage:
        def compute_file_hash(self, *, media):
            assert media.media_id > 0
            assert media.storage_ref == staged.storage_ref
            assert media.file_size_bytes == staged.size_bytes
            return file_hash

    media = MediaImportService._create_media(
        storage=Storage(),
        movie=movie,
        video_item=None,
        library=library,
        source=source,
        staged=staged,
    )

    assert Media.get_by_id(media.id).file_hash == file_hash
