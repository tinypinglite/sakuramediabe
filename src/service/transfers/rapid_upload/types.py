from dataclasses import dataclass


class RapidUploadFailure(RuntimeError):
    """携带稳定 failure_reason 的条目级失败。"""

    def __init__(self, message: str, *, failure_reason: str) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


@dataclass(frozen=True)
class ItemSpec:
    media_id: int
    action: str
    source_library_id: int | None
    source_path: str
    source_size_bytes: int
    source_mtime_ns: int
    source_sha1: str | None = None
    target_cid: str | None = None
    target_fid: str | None = None
    target_pickcode: str | None = None
    target_name: str | None = None
    retry_source_item_id: int | None = None
