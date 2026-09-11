import time
from copy import deepcopy
from threading import Event, Lock, Thread

from loguru import logger

from src.model import get_database


class ThumbnailTaskProgress:
    """Keep thumbnail work visible while provider calls block."""

    INTERVAL_SECONDS = 2

    def __init__(self, reporter):
        self.reporter = reporter
        self.payload = None
        self.changed_at = time.monotonic()
        self.last_emit_at = 0.0
        self.lock = Lock()
        self.stop = Event()
        self.thread = Thread(target=self._heartbeat, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join()

    def emit(self, *, force=True, **payload):
        with self.lock:
            if self.payload != payload:
                self.changed_at = time.monotonic()
            self.payload = deepcopy(payload)
            if force or time.monotonic() - self.last_emit_at >= self.INTERVAL_SECONDS:
                self._emit()

    def _emit(self):
        payload = dict(self.payload)
        elapsed = int(time.monotonic() - self.changed_at)
        if elapsed >= self.INTERVAL_SECONDS:
            payload["text"] += f" · 本步骤已等待 {elapsed} 秒"
        self.reporter.emit(**payload)
        self.last_emit_at = time.monotonic()

    def _heartbeat(self):
        while not self.stop.wait(self.INTERVAL_SECONDS):
            try:
                with self.lock:
                    if self.payload and time.monotonic() - self.last_emit_at >= self.INTERVAL_SECONDS:
                        with get_database().connection_context():
                            self._emit()
            except Exception:
                logger.exception("Thumbnail task progress heartbeat failed")
