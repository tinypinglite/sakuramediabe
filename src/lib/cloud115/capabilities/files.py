from __future__ import annotations

from typing import Any

from src.lib.cloud115.capabilities.base import Cloud115Capability
from src.common.service_helpers import poll_until
from src.lib.cloud115.exceptions import Cloud115NotFoundError, Cloud115RequestError
from src.lib.cloud115.types import DirBreadcrumb, DirEntry, DirMeta, FileMeta


class FilesCapability(Cloud115Capability):
    _BASE_WEBAPI = "https://webapi.115.com"
    _LIST_DIR_MAX_LIMIT = 1150
    _PICKCODE_INDEX_WAIT_DELAYS = (0.0, 0.3, 0.8, 1.5, 2.5)

    async def list_dir(
        self,
        cid: str = "0",
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> tuple[list[DirEntry], int]:
        """列目录一页。返回 (当前批次条目, 目录总数)。

        cid: 目录 category_id 字符串，根目录用 "0"。
        limit: 单页大小，服务端硬上限 1150；超限抛 ValueError（call site bug，不做静默截断）。
        """
        if limit > self._LIST_DIR_MAX_LIMIT:
            raise ValueError(
                f"list_dir limit {limit} exceeds server max {self._LIST_DIR_MAX_LIMIT}"
            )
        url = f"{self._BASE_WEBAPI}/files"
        params = {
            "aid": 1,
            "cid": cid,
            "offset": offset,
            "limit": limit,
            "show_dir": 1,
        }
        payload = await self._request_json("GET", url, params=params)
        if not payload.get("state"):
            # state=False 时统一按 errno 映射（990002 = 父目录不存在 -> NotFound；auth 类 -> AuthError）
            raise self._map_errno(payload, endpoint=url)
        entries = [self._parse_dir_entry(raw) for raw in (payload.get("data") or [])]
        total = int(payload.get("count", 0))
        return entries, total

    async def file_info(self, file_id: str) -> FileMeta:
        """取单文件元信息。file_id 是整数字符串（list_dir 结果里的 entry_id）。

        业务侧通常持久化的是 pickcode（跨会话稳定）而不是 file_id；如果只有 pickcode
        请用 pickcode_info。
        """
        if not file_id:
            raise ValueError("file_id is required")
        return await self._get_info(param_key="file_id", param_value=file_id, human_id=file_id)

    async def pickcode_info(self, pickcode: str) -> FileMeta:
        """按 pickcode 查文件元信息。走同一 /files/get_info 端点，只是参数名换成 pick_code。

        pickcode 是业务侧的稳定 ID；file_id 会因 115 内部存储位置变动而变化。
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        return await self._get_info(param_key="pick_code", param_value=pickcode, human_id=pickcode)

    async def _wait_pickcode_indexed(self, pickcode: str) -> FileMeta:
        result: list[FileMeta] = []

        async def _check() -> None:
            result.append(await self.pickcode_info(pickcode))

        await poll_until(self._PICKCODE_INDEX_WAIT_DELAYS, _check)
        return result[0]

    async def _get_info(self, *, param_key: str, param_value: str, human_id: str) -> FileMeta:
        url = f"{self._BASE_WEBAPI}/files/get_info"
        payload = await self._request_json("GET", url, params={param_key: param_value})
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        data = payload.get("data") or []
        if not data:
            raise Cloud115NotFoundError(
                f"{param_key}={human_id} not found", endpoint=url
            )
        return self._parse_file_meta(data[0])

    async def dir_info(self, cid: str) -> DirMeta:
        """取目录元信息 + 面包屑。

        cid="0" 会被 115 服务端拒（errNo=1001），SDK 层直接构造哨兵返回（name="根目录"、
        pickcode=""、paths=()），调用方不必特判。

        端点：GET webapi.115.com/category/get?cid=<cid>
        """
        if not cid:
            raise ValueError("cid is required")
        if cid == "0":
            # 根目录哨兵
            return DirMeta(
                cid="0",
                name="根目录",
                pickcode="",
                parent_id="",
                file_count=0,
                folder_count=0,
                play_long_seconds=0,
                mtime=0,
                ctime=0,
                paths=(),
            )
        url = f"{self._BASE_WEBAPI}/category/get"
        payload = await self._request_json("GET", url, params={"cid": cid})
        # category/get 的失败态：state=false + errNo=1001 参数错 / cid 不存在
        # 与 list_dir 的 state 判定风格保持一致
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        return self._parse_dir_meta(cid, payload)

    async def mkdir(self, pid: str, name: str) -> str:
        """在 pid 目录下建一个叫 name 的子目录，返回新目录 cid。

        - pid: 父目录 cid，根用 "0"。name: 目录名（不做前后空格清理，上层负责）。
        - 重名会被拒绝：同一父目录下已有同名目录时返回 HTTP 200 + state=false + errno=20004
          （2026-07-29 实测），既不幂等返回既有 cid、也不建出重名目录，映射为
          ``Cloud115DuplicateNameError``。上层可以乐观 mkdir、撞到本异常再定位复用
          （见 ``find_or_create_subdir``）。注意别的写入路径（转存、云下载、上传）仍可能
          造成同名目录并存，只有 files/add 这一条会拒绝。
        - 端点：POST webapi.115.com/files/add，body {pid, cname}。
        """
        if not name:
            raise ValueError("name is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/add"
        payload = await self._request_json(
            "POST", url, data={"pid": pid, "cname": name}
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        # 115 成功响应字段名有 category_id / cid / file_id 三种历史写法，兜住任一
        cid = str(
            payload.get("category_id")
            or payload.get("cid")
            or payload.get("file_id")
            or ""
        )
        if not cid:
            raise Cloud115RequestError(
                "mkdir response missing new cid",
                method="POST",
                url=url,
                detail=str(payload)[:200],
            )
        return cid

    async def iter_files_recursive(
        self,
        cid: str,
        *,
        page_size: int = _LIST_DIR_MAX_LIMIT,
    ):
        """递归枚举 cid 目录树下的**全部文件**（不含目录条目），逐条 yield DirEntry。

        - 触发条件：/files 加 show_dir=0 & cur=0（p115client 记载的全树递归模式）。
        - 递归模式下每条只带 parent cid（parent_id），**拿不到父目录名** ——
          cid→目录名映射由上层自己用 list_dir 遍历目录结构维护（目录数远小于文件数）。
        - play_long / ic 字段在本响应里白给，导入侧直接消费，无需逐文件再查。
        """
        if not cid:
            raise ValueError("cid is required (use '0' for root)")
        if page_size > self._LIST_DIR_MAX_LIMIT:
            raise ValueError(
                f"page_size {page_size} exceeds server max {self._LIST_DIR_MAX_LIMIT}"
            )
        url = f"{self._BASE_WEBAPI}/files"
        offset = 0
        total = -1
        while total < 0 or offset < total:
            params = {
                "aid": 1,
                "cid": cid,
                "offset": offset,
                "limit": page_size,
                "show_dir": 0,
                "cur": 0,
                "o": "file_name",
                "asc": 1,
            }
            payload = await self._request_json("GET", url, params=params)
            if not payload.get("state"):
                raise self._map_errno(payload, endpoint=url)
            batch = [self._parse_dir_entry(raw) for raw in (payload.get("data") or [])]
            total = int(payload.get("count", 0))
            if not batch:
                break
            for entry in batch:
                yield entry
            offset += len(batch)

    async def copy_files(self, fids: list[str], *, pid: str) -> None:
        """批量复制文件/目录到 pid 目录（云端零流量搬运）。

        - ⚠️ 115 文档明确：copy 勿并发执行、单次 ≤5 万个 → 上层串行分批调用本方法。
        - **复制产生新 fid 和新 pickcode**（仅 sha1 相同）→ 登记必须以复制后
          re-list 目标目录拿到的新条目为准，不能拿源条目的 pickcode 落库。
        - 同账号内复制占双倍空间。
        - 端点：POST webapi.115.com/files/copy，body {pid, fid[0..n]}。
        """
        if not fids:
            raise ValueError("fids is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/copy"
        data: dict[str, Any] = {"pid": pid}
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def move_files(self, fids: list[str], *, pid: str) -> None:
        """批量移动文件/目录到 pid 目录。

        - 与 copy 协议同型；**移动保持 fid / pickcode 不变** → 登记可直接用源条目，
          且可以在移动之前就完成（Media 靠 pickcode 定位，与所在目录无关）。
        - 不占双倍空间；媒体导入的 cleanup-source 走的就是本方法。
        - 端点：POST webapi.115.com/files/move，body {pid, fid[0..n]}。
        """
        if not fids:
            raise ValueError("fids is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/move"
        data: dict[str, Any] = {"pid": pid}
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def batch_rename(self, renames: dict[str, str]) -> None:
        """批量改名。renames: {fid: 新名}。

        - ⚠️ 文件新名**必须带扩展名**：115 会把最后一个 '.' 之后的部分截断处理，
          不带扩展名会导致名字被意外截断；且扩展名本身不可改。
        - 改名保持 fid / pickcode 不变。
        - 单批条数上限未见官方文档，上层保守按 30–50/批分批。
        - 端点：POST webapi.115.com/files/batch_rename，body files_new_name[<fid>]=<新名>。
        """
        if not renames:
            raise ValueError("renames is required")
        for fid, new_name in renames.items():
            if not fid or not new_name:
                raise ValueError(f"invalid rename entry: {fid!r} -> {new_name!r}")
        url = f"{self._BASE_WEBAPI}/files/batch_rename"
        data = {f"files_new_name[{fid}]": name for fid, name in renames.items()}
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def rename_file(self, fid: str, new_name: str) -> None:
        """单文件改名；每次请求只提交一个 fid，供需逐项确认的导入流程使用。"""
        if not fid or not new_name:
            raise ValueError(f"invalid rename entry: {fid!r} -> {new_name!r}")
        url = f"{self._BASE_WEBAPI}/files/batch_rename"
        payload = await self._request_json(
            "POST", url, data={f"files_new_name[{fid}]": new_name}
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def delete_files(self, fids: list[str], *, pid: str | None = None) -> None:
        """批量删除文件/目录（进 115 回收站，有误删缓冲）。

        - pid 可选：传删除项所在父目录 cid 可少一次服务端定位（不传也能删）。
        - 端点：POST webapi.115.com/rb/delete，body {fid[0..n], pid?}。
        """
        if not fids:
            raise ValueError("fids is required")
        url = f"{self._BASE_WEBAPI}/rb/delete"
        data: dict[str, Any] = {}
        if pid:
            data["pid"] = pid
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    @staticmethod
    def _parse_dir_entry(raw: dict[str, Any]) -> DirEntry:
        """115 短字段名 -> DirEntry。fid 存在 => 文件，缺失 => 目录。"""
        is_dir = "fid" not in raw
        if is_dir:
            # 目录的 category_id 是自己的 cid，parent 是 pid
            entry_id = str(raw.get("cid", ""))
            parent_id = str(raw.get("pid", ""))
        else:
            # 文件的 file_id 是 fid，parent 是 cid
            entry_id = str(raw.get("fid", ""))
            parent_id = str(raw.get("cid", ""))
        # play_long / ic 是可选字段：未转码视频、目录、旧响应可能不带，缺省一律 None
        play_long_raw = raw.get("play_long")
        ic_raw = raw.get("ic")
        return DirEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            name=str(raw.get("n", "")),
            is_dir=is_dir,
            size=int(raw.get("s") or 0),
            sha1=str(raw["sha"]) if raw.get("sha") else None,
            pickcode=str(raw.get("pc", "")),
            mtime=int(raw.get("te") or 0),
            ctime=int(raw.get("tp") or 0),
            is_video=bool(raw.get("iv")) if not is_dir else False,
            play_long=int(float(play_long_raw)) if play_long_raw not in (None, "") else None,
            ic=int(ic_raw) if ic_raw not in (None, "") else None,
        )

    @staticmethod
    def _parse_file_meta(raw: dict[str, Any]) -> FileMeta:
        """get_info 单条 -> FileMeta。字段与目录条目短名一致但只覆盖文件字段。"""
        return FileMeta(
            file_id=str(raw.get("fid") or raw.get("file_id") or ""),
            parent_id=str(raw.get("cid", "")),
            name=str(raw.get("n", "")),
            size=int(raw.get("s") or 0),
            sha1=str(raw.get("sha", "")),
            pickcode=str(raw.get("pc", "")),
            mtime=int(raw.get("te") or 0),
            ctime=int(raw.get("tp") or 0),
            is_video=bool(raw.get("iv")),
        )

    @staticmethod
    def _parse_dir_meta(cid: str, payload: dict[str, Any]) -> DirMeta:
        """/category/get 响应 -> DirMeta。

        字段名（观察自 2026-07-12 真实响应）：
            file_name / pick_code / paths[] / count / folder_count / play_long / ctime / utime
        parent_id 从 paths 末尾解析；paths 是从根目录到当前目录父级的面包屑链。
        """
        raw_paths = payload.get("paths") or []
        crumbs: list[DirBreadcrumb] = []
        for item in raw_paths:
            if not isinstance(item, dict):
                continue
            # 根目录 file_id 是数字 0，不能用 `x or ""` 吞掉；显式挑存在的字段
            fid_raw = item.get("file_id") if "file_id" in item else item.get("cid")
            name_raw = item.get("file_name") if "file_name" in item else item.get("name")
            crumbs.append(
                DirBreadcrumb(
                    file_id="" if fid_raw is None else str(fid_raw),
                    name="" if name_raw is None else str(name_raw),
                )
            )
        # 父目录 cid 从面包屑末尾拿；如果 paths 为空（少见）则空串
        parent_id = crumbs[-1].file_id if crumbs else ""
        return DirMeta(
            cid=cid,
            name=str(payload.get("file_name", "") or ""),
            pickcode=str(payload.get("pick_code", "") or ""),
            parent_id=parent_id,
            file_count=int(payload.get("count") or 0),
            folder_count=int(payload.get("folder_count") or 0),
            play_long_seconds=int(payload.get("play_long") or 0),
            mtime=int(payload.get("utime") or 0),
            ctime=int(payload.get("ctime") or 0),
            paths=tuple(crumbs),
        )
