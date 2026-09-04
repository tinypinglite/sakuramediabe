"""Short-lived ownership of media I/O and library configuration/inventory."""

from contextlib import contextmanager

from src.api.exception.errors import ApiError
from src.model import get_database

MEDIA_LOCK = 17001
LIBRARY_LOCK = 17002


class MediaOperationBusy(ApiError):
    def __init__(self):
        super().__init__(
            409, "media_operation_busy", "媒体或媒体库正在处理，请稍后重试"
        )


@contextmanager
def media_operation_lock(namespace: int, resource_id: int):
    # Media/MediaLibrary use PostgreSQL serial (signed int32), with separate namespaces.
    if not 0 < resource_id < 2**31:
        raise ValueError("invalid media operation lock id")
    database = get_database()
    connection = database.connection()

    def check_connection():
        if (
            connection.closed
            or database.is_closed()
            or database.connection() is not connection
        ):
            raise RuntimeError("media_operation_connection_lost")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (namespace, resource_id))
        if not cursor.fetchone()[0]:
            raise MediaOperationBusy()
    try:
        yield check_connection
    finally:
        # Release on the original session only, never on an automatically reconnected one.
        if not connection.closed:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s, %s)", (namespace, resource_id)
                    )
            except Exception:
                connection.close()
