"""cloud115 Media 记录写入的统一入口。

三个管线（秒传、JAV 导入、视频导入）都通过本模块构造 locator / fingerprint、
查询库内已有 Media、写入或创建 cloud115 存储字段。新增 cloud115 相关 Media 字段时
只需改本模块，不必同步三处。
"""

from __future__ import annotations

from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary


class Cloud115MediaRegistrar:

    # ---- 构造原语 ----

    @staticmethod
    def build_locator(
        *, fid: str, pickcode: str, name: str, source_path: str
    ) -> dict:
        """构造规范的 backend_locator；键序固定，(library, locator) 唯一索引依赖此序。"""
        return {
            "fid": fid,
            "pickcode": pickcode,
            "name": name,
            "source_path": source_path,
        }

    @staticmethod
    def build_fingerprint(sha1: str) -> str:
        return f"sha1:{sha1.upper()}"

    # ---- 查询 ----

    @classmethod
    def find_library_media(
        cls, library: MediaLibrary, sha1: str, *, valid: bool | None = None
    ) -> Media | None:
        """按 sha1 指纹查找库内 Media；valid=None 不限有效性。

        走 build_fingerprint 归一化查询串，与写入端保持 uppercase 一致；否则调用方
        传入本地算出的小写 hexdigest 会漏命中已有大写指纹记录，导致重复登记或触发唯一
        索引冲突。
        """
        query = Media.select().where(
            Media.library == library,
            Media.content_fingerprint == cls.build_fingerprint(sha1),
        )
        if valid is not None:
            query = query.where(Media.valid == valid)
        return query.order_by(Media.id.desc()).first()

    @classmethod
    def find_movie_source_media(
        cls,
        library: MediaLibrary,
        movie,
        sha1: str,
        *,
        locator: dict,
        valid: bool | None = None,
        for_update: bool = False,
    ) -> Media | None:
        """精确查找当前 JAV 影片登记的同一远端源文件。

        指纹可以对应多条 Media，续搬不能只凭 SHA1 或 fid 猜测归属；locator 由统一
        builder 构造并受同库唯一索引保护，可作为事务内复用的精确身份。
        """
        query = Media.select().where(
            Media.library == library,
            Media.movie == movie.movie_number,
            Media.video_item.is_null(True),
            Media.content_fingerprint == cls.build_fingerprint(sha1),
            Media.backend_locator == locator,
        )
        if valid is not None:
            query = query.where(Media.valid == valid)
        if for_update:
            query = query.for_update()
        return query.order_by(Media.id.desc()).first()

    # ---- 写入 ----

    @staticmethod
    def apply_cloud115_fields(
        media: Media,
        *,
        library: MediaLibrary,
        locator: dict,
        fingerprint: str,
        file_size_bytes: int,
        storage_mode: str | None = None,
        resolution: str | None = None,
        duration_seconds: int = 0,
        video_info: dict | None = None,
        special_tags: str | None = None,
        valid: bool | None = None,
    ) -> None:
        """把 cloud115 存储字段写入已有 Media 实例（不调 save，由调用方控制事务）。

        可选字段为 None / 0 时不覆盖，适用于"只更新有新值的列"的 update 场景。
        """
        media.library = library
        media.path = None
        media.backend_locator = locator
        media.content_fingerprint = fingerprint
        media.file_size_bytes = file_size_bytes
        if storage_mode is not None:
            media.storage_mode = storage_mode
        if resolution is not None:
            media.resolution = resolution
        if duration_seconds > 0:
            media.duration_seconds = duration_seconds
        if video_info is not None:
            media.video_info = video_info
        if special_tags is not None:
            media.special_tags = special_tags
        if valid is not None:
            media.valid = valid
        media.updated_at = utc_now_for_db()

    @staticmethod
    def create_cloud115_media(
        *,
        library: MediaLibrary,
        locator: dict,
        fingerprint: str,
        file_size_bytes: int,
        movie=None,
        video_item=None,
        storage_mode: str | None = None,
        resolution: str | None = None,
        duration_seconds: int = 0,
        video_info: dict | None = None,
        special_tags: str | None = None,
        valid: bool = True,
    ) -> Media:
        """创建一条指向 cloud115 的新 Media 记录。"""
        return Media.create(
            movie=movie,
            video_item=video_item,
            library=library,
            backend_locator=locator,
            content_fingerprint=fingerprint,
            file_size_bytes=file_size_bytes,
            storage_mode=storage_mode,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_info=video_info,
            special_tags=special_tags,
            valid=valid,
        )
