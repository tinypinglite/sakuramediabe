"""跨导入链路复用的媒体库写入作用域键。"""

from src.model import MediaLibrary


def library_import_scope_key(*, library: MediaLibrary) -> str:
    """同库导入/迁移任务按此键 FIFO 排队；执行层保证同键最多一个 running。"""
    return f"library_import:{library.id}"
