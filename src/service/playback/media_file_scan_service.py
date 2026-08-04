from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.service_helpers import emit_progress
from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary


@dataclass(frozen=True)
class MediaFileCheckResult:
    id: int
    path: str
    file_exists: bool
    valid_before: bool
    valid_after: bool
    updated: bool
    invalidated: bool
    revived: bool
    checked_at: datetime


class MediaFileScanService:
    DEFAULT_BATCH_SIZE = 100

    @staticmethod
    def _build_candidate_query(
        last_media_id: int = 0,
        max_media_id: int | None = None,
    ):
        conditions = [Media.id > last_media_id]
        if max_media_id is not None:
            conditions.append(Media.id <= max_media_id)
        return (
            Media.select(Media)
            .where(*conditions)
            .order_by(Media.id.asc())
        )

    @staticmethod
    def _max_media_id() -> int:
        """固定本轮扫描上界，避免把建立远端快照后新导入的媒体误判为不存在。"""
        return (
            Media.select(Media.id).order_by(Media.id.desc()).limit(1).scalar()
            or 0
        )

    @staticmethod

    @staticmethod
    def _cloud115_library_ids() -> set[int]:
        """一次查出全部 cloud115 库 id，取代逐条媒体懒加载 media.library 的 N+1。"""
        from src.model.enums import MediaLibraryBackend

        return {
            library.id
            for library in MediaLibrary.select(MediaLibrary.id).where(
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value
            )
        }

    @classmethod
    def _is_cloud115_media(cls, media: Media, cloud115_library_ids: set[int]) -> bool:
        return media.library_id is not None and media.library_id in cloud115_library_ids

    @staticmethod
    def _build_cloud115_remote_index(
        libraries: list[MediaLibrary],
    ) -> dict[int, set[str]]:
        """整树枚举每个 cloud115 库的受管根，返回 ``{library_id: {pickcode}}``。

        请求数是 ``ceil(受管文件数 / 1150)``，与目录层数无关——115 的 ``GET /files``
        在 ``show_dir=0 & cur=0`` 下由服务端展开整棵子树、只返回文件。枚举范围严格限定
        在库配置的 ``root_cid`` 子树内，不触碰网盘上的其它目录。

        枚举失败的库**不进结果字典**，调用方据此让该库媒体整轮跳过、不动 valid。
        这是与逐条 pickcode_info 探测的本质区别：逐条探测下每条媒体各自面对一次限流，
        而限流回的 errno 与"资源被封禁"重叠（见 transport 的 _NOT_FOUND_ERRNOS），
        会被误判成文件不存在并批量标 invalid；整树枚举要么拿到完整清单、要么整轮放弃，
        不存在部分误判。
        """
        import asyncio

        from src.lib.cloud115 import Cloud115Error
        from src.service.cloud115 import cloud115_client_for, require_cloud115_library

        async def _enumerate(library: MediaLibrary) -> set[str]:
            root_cid = require_cloud115_library(library)["root_cid"]
            pickcodes: set[str] = set()
            async with cloud115_client_for(library, batch_pacing=True) as client:
                async for entry in client.iter_files_recursive(root_cid):
                    if entry.pickcode:
                        pickcodes.add(entry.pickcode)
            return pickcodes

        index: dict[int, set[str]] = {}
        for library in libraries:
            try:
                pickcodes = asyncio.run(_enumerate(library))
            except (Cloud115Error, ValueError) as exc:
                logger.warning(
                    "Cloud115 remote index build failed library_id={} detail={}",
                    library.id,
                    exc,
                )
                continue
            index[library.id] = pickcodes
            logger.info(
                "Cloud115 remote index built library_id={} remote_files={}",
                library.id,
                len(pickcodes),
            )
        return index

    def _scan_cloud115_media(
        self,
        media: Media,
        *,
        remote_index: dict[int, set[str]] | None = None,
    ) -> dict[str, bool | datetime]:
        """cloud115 媒体对账：判断受管文件在远端是否仍然存在。

        - 批量巡检传入 ``remote_index``（整树枚举结果）：命中与否是纯内存判定，零请求。
          库不在 index 里说明本轮枚举失败，直接跳过不动 valid。
        - 单条复查不传 index：退回 ``pickcode_info`` 单点探测（1 条媒体 1 个请求），
          此时 NotFound → 标 invalid，其余上游错误 → 跳过不动 valid。
        """
        import asyncio

        from src.lib.cloud115 import Cloud115Error, Cloud115NotFoundError
        from src.service.cloud115 import cloud115_client_for

        checked_at = utc_now_for_db()
        result = {
            "updated": False,
            "invalidated": False,
            "revived": False,
            "file_exists": bool(media.valid),
            "checked_at": checked_at,
        }
        pickcode = (media.backend_locator or {}).get("pickcode")
        if not pickcode:
            logger.warning("Cloud115 media missing pickcode media_id={}", media.id)
            return result

        if remote_index is not None:
            remote_pickcodes = remote_index.get(media.library_id)
            if remote_pickcodes is None:
                # 本库整树枚举失败：清单不可信，本轮不对该库做任何 valid 判定。
                return result
            file_exists = pickcode in remote_pickcodes
        else:
            async def _probe() -> bool:
                async with cloud115_client_for(media.library) as client:
                    await client.pickcode_info(pickcode)
                    return True

            try:
                file_exists = asyncio.run(_probe())
            except Cloud115NotFoundError:
                file_exists = False
            except Cloud115Error as exc:
                # 上游不可用：本轮跳过，保持现状（不误标 invalid）。
                logger.warning(
                    "Cloud115 media probe skipped media_id={} detail={}", media.id, exc
                )
                return result

        result["file_exists"] = file_exists
        if media.valid != file_exists:
            media.valid = file_exists
            media.updated_at = checked_at
            media.save(only=[Media.valid, Media.updated_at])
            result["updated"] = True
            result["invalidated"] = not file_exists
            result["revived"] = file_exists
        return result

    def _scan_single_media(
        self,
        media: Media,
        *,
        cloud115_library_ids: set[int],
        remote_index: dict[int, set[str]] | None = None,
    ) -> dict[str, bool | datetime]:
        if self._is_cloud115_media(media, cloud115_library_ids):
            return self._scan_cloud115_media(media, remote_index=remote_index)
        file_path = Path(media.path).expanduser().resolve()
        file_exists = file_path.exists() and file_path.is_file()
        checked_at = utc_now_for_db()
        updates: dict = {}
        result = {
            "updated": False,
            "invalidated": False,
            "revived": False,
            "file_exists": file_exists,
            "checked_at": checked_at,
        }

        if media.valid != file_exists:
            # valid 的语义以当前文件状态为准，巡检要把库里状态修正回来。
            updates[Media.valid] = file_exists
            result["invalidated"] = not file_exists
            result["revived"] = file_exists

        if not updates:
            return result

        for field, value in updates.items():
            setattr(media, field.name, value)
        media.updated_at = checked_at
        media.save(only=[*updates.keys(), Media.updated_at])
        result["updated"] = True
        return result

    def check_media_file(self, media_id: int) -> MediaFileCheckResult:
        media = Media.get_or_none(Media.id == media_id)
        if media is None:
            raise ApiError(
                404,
                "media_not_found",
                "Media not found",
                {"media_id": media_id},
            )

        valid_before = bool(media.valid)
        # 单条复查不建整树索引：为一条媒体枚举整个库不划算，走单点探测即可。
        result = self._scan_single_media(
            media, cloud115_library_ids=self._cloud115_library_ids()
        )
        media = Media.get_by_id(media.id)
        # 单条复查直接返回状态变化，前端无需再推断 valid 是否被本次修正。
        return MediaFileCheckResult(
            id=media.id,
            path=media.display_path,
            file_exists=bool(result["file_exists"]),
            valid_before=valid_before,
            valid_after=bool(media.valid),
            updated=bool(result["updated"]),
            invalidated=bool(result["invalidated"]),
            revived=bool(result["revived"]),
            checked_at=result["checked_at"],
        )

    def scan_media_files(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_callback=None,
    ) -> dict[str, int]:
        stats = {
            "scanned_media": 0,
            "updated_media": 0,
            "skipped_media": 0,
            "failed_media": 0,
            "invalidated_media": 0,
            "revived_media": 0,
            "cloud115_index_failed_libraries": 0,
        }
        max_media_id = self._max_media_id()
        last_media_id = 0
        emit_progress(
            progress_callback,
            current=0,
            total=None,
            text="开始巡检媒体文件",
            summary_patch=stats,
        )

        from src.model.enums import MediaLibraryBackend

        cloud115_libraries = list(
            MediaLibrary.select().where(
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value
            )
        )
        cloud115_library_ids = {library.id for library in cloud115_libraries}
        # 整树枚举一次拿到各 cloud115 库的远端文件清单，后续逐条对账全在内存里完成。
        remote_index = (
            self._build_cloud115_remote_index(cloud115_libraries)
            if cloud115_libraries
            else {}
        )
        stats["cloud115_index_failed_libraries"] = len(cloud115_library_ids) - len(
            remote_index
        )
        if stats["cloud115_index_failed_libraries"]:
            # 枚举失败的库本轮不做任何 valid 判定，必须显式暴露，
            # 否则结果看起来与"全部正常、无变化"完全一样。
            logger.warning(
                "Cloud115 remote index incomplete: {}/{} libraries enumerated, "
                "their media are skipped this round",
                len(remote_index),
                len(cloud115_library_ids),
            )
        emit_progress(
            progress_callback,
            current=0,
            total=None,
            text="远端文件清单枚举完成",
            summary_patch=stats,
        )

        while True:
            candidates = list(
                self._build_candidate_query(last_media_id, max_media_id).limit(
                    max(1, batch_size)
                )
            )
            if not candidates:
                return stats

            for media in candidates:
                last_media_id = media.id
                stats["scanned_media"] += 1
                try:
                    result = self._scan_single_media(
                        media,
                        cloud115_library_ids=cloud115_library_ids,
                        remote_index=remote_index,
                    )
                except Exception as exc:
                    stats["failed_media"] += 1
                    logger.warning(
                        "Scan media file failed media_id={} path={} detail={}",
                        media.id,
                        media.path,
                        exc,
                    )
                    emit_progress(
                        progress_callback,
                        current=stats["scanned_media"],
                        total=None,
                        text=f"媒体文件巡检失败 media_id={media.id}",
                        summary_patch=stats,
                    )
                    continue

                if result["updated"]:
                    stats["updated_media"] += 1
                else:
                    stats["skipped_media"] += 1
                if result["invalidated"]:
                    stats["invalidated_media"] += 1
                if result["revived"]:
                    stats["revived_media"] += 1

                emit_progress(
                    progress_callback,
                    current=stats["scanned_media"],
                    total=None,
                    text=f"已巡检媒体文件 media_id={media.id}",
                    summary_patch=stats,
                )
