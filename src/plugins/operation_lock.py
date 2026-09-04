"""Coordinate plugin directory mutations with active media transfers."""

from contextlib import contextmanager
from pathlib import Path

import portalocker

from src.api.exception.errors import ApiError


@contextmanager
def plugin_operation_lock(root: Path, *, shared: bool = False):
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking the lock file would let another process bypass it.
    with (root / ".media-transfer.lock").open("a") as handle:
        try:
            portalocker.lock(
                handle,
                (portalocker.LOCK_SH if shared else portalocker.LOCK_EX)
                | portalocker.LOCK_NB,
            )
        except portalocker.exceptions.AlreadyLocked:
            raise ApiError(
                409, "plugin_operation_busy", "插件管理或媒体迁移正在执行，请稍后重试"
            ) from None
        try:
            yield
        finally:
            portalocker.unlock(handle)
