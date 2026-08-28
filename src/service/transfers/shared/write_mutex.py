"""Host-side import write mutex."""

from src.model import MediaLibrary


def library_import_mutex_key(*, library: MediaLibrary) -> str:
    return f"library_import:{library.id}"
