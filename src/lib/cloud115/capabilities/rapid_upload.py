from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Literal

from src.lib.cloud115.capabilities.base import Cloud115Capability
from src.lib.cloud115.cipher import decrypt_upload_response, make_upload_payload
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115Error,
    Cloud115RequestError,
)
from src.lib.cloud115.types import RapidUploadResult, RapidUploadStatus


class RapidUploadCapability(Cloud115Capability):
    _BASE_PROAPI = "https://proapi.115.com"
    _BASE_UPLOAD = "https://uplb.115.com"
    _UPLOAD_APP_VERSION_URL = "https://appversion.115.com/1.0/web/1.0/api/getMultiVer"
    _UID_SSOENT_PATTERN = re.compile(r"^\d+_([A-Z]\d)_")
    _RAPID_UPLOAD_PROTOCOL_BY_SSOENT = {"F1": "android", "R2": "web"}

    def __init__(self, transport, files) -> None:
        super().__init__(transport)
        self._files = files
        self._upload_userkeys: dict[str, str] = {}
        self._upload_userkey_lock = asyncio.Lock()
        self._upload_app_version: str | None = None
        self._upload_app_version_lock = asyncio.Lock()

    async def _wait_pickcode_indexed(self, pickcode: str):
        return await self._files._wait_pickcode_indexed(pickcode)

    async def rapid_upload(
        self,
        path: str | Path,
        *,
        pid: str = "0",
    ) -> RapidUploadResult:
        """只尝试秒传一个本地文件，不会回退到普通上传。

        大文件首次初始化可能返回 status=7，要求读取服务端指定范围并再次提交范围哈希；
        最终 status=2 才表示文件已落到目标目录。SDK 根据 UID Cookie 自动识别 Android
        F1 或支付宝小程序 R2 槽，并选择对应的 userkey 接口；其他槽位不支持秒传。
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"file path is not a regular file: {file_path}")
        if not pid:
            raise ValueError("pid is required")
        upload_protocol = self._rapid_upload_protocol()

        before = self._file_snapshot(file_path)
        size = before[0]
        file_sha1 = (await asyncio.to_thread(self._hash_file, file_path)).upper()
        if self._file_snapshot(file_path) != before:
            return RapidUploadResult(
                status=RapidUploadStatus.FILE_CHANGED,
                path=str(file_path),
                filename=file_path.name,
                size=size,
                sha1=file_sha1,
            )

        response = await self._upload_init(
            filename=file_path.name,
            filesize=size,
            filesha1=file_sha1,
            pid=pid,
            upload_protocol=upload_protocol,
        )
        data = self._upload_data(response)
        status = int(data.get("status") or 0)
        if status == 7:
            sign_key = str(data.get("sign_key") or "")
            sign_check = str(data.get("sign_check") or "")
            if not sign_key or not sign_check:
                raise Cloud115RequestError(
                    "upload init status=7 missing sign_key/sign_check",
                    method="POST",
                    url=f"{self._BASE_UPLOAD}/4.0/initupload.php",
                )
            if self._file_snapshot(file_path) != before:
                return RapidUploadResult(
                    status=RapidUploadStatus.FILE_CHANGED,
                    path=str(file_path),
                    filename=file_path.name,
                    size=size,
                    sha1=file_sha1,
                    raw_response=response,
                )
            range_sha1 = await asyncio.to_thread(
                self._hash_file_range,
                file_path,
                sign_check,
            )
            if self._file_snapshot(file_path) != before:
                return RapidUploadResult(
                    status=RapidUploadStatus.FILE_CHANGED,
                    path=str(file_path),
                    filename=file_path.name,
                    size=size,
                    sha1=file_sha1,
                    raw_response=response,
                )
            response = await self._upload_init(
                filename=file_path.name,
                filesize=size,
                filesha1=file_sha1,
                pid=pid,
                sign_key=sign_key,
                sign_val=range_sha1,
                upload_protocol=upload_protocol,
            )
            data = self._upload_data(response)
            status = int(data.get("status") or 0)

        if status != 2:
            return RapidUploadResult(
                status=RapidUploadStatus.NOT_HIT,
                path=str(file_path),
                filename=file_path.name,
                size=size,
                sha1=file_sha1,
                raw_response=response,
            )
        # initupload 的 status=2 响应里 pickcode 才是真实稳定标识；同响应里
        # 的 fileid 字段是 int 占位符（多为 0，见 SheltonZhu/115driver 里
        # "Useless fields" 注释与 ChenyangGao/p115client 的处理）。业务层需要
        # 真 file_id 走 rename/delete/file_info，只能靠 pickcode 反查。
        pickcode = self._first_text(data, "pick_code", "pickcode")
        if not pickcode:
            raise Cloud115RequestError(
                "upload init status=2 missing pickcode",
                method="POST",
                url=f"{self._BASE_UPLOAD}/4.0/initupload.php",
                detail=str(data)[:200],
            )
        # 新落地文件的索引在 115 侧不是即时的：initupload 刚返回 status=2 就查
        # pickcode 常常撞上 Cloud115NotFoundError（data=[]）。做短退避重试，跟
        # verify_cloud115_renamed_file 一样只兜索引窗口，其它错误立刻透传出去。
        meta = await self._wait_pickcode_indexed(pickcode)
        return RapidUploadResult(
            status=RapidUploadStatus.SUCCESS,
            path=str(file_path),
            filename=file_path.name,
            size=size,
            sha1=file_sha1,
            file_id=meta.file_id,
            pickcode=meta.pickcode or pickcode,
            raw_response=response,
        )

    def _rapid_upload_protocol(self) -> Literal["web", "android"]:
        """从 UID Cookie 的登录槽自动选择秒传所需的 userkey 接口。"""
        uid = self._cookies_dict.get("UID", "")
        match = self._UID_SSOENT_PATTERN.match(uid)
        ssoent = match.group(1) if match else ""
        protocol = self._RAPID_UPLOAD_PROTOCOL_BY_SSOENT.get(ssoent)
        if protocol == "android":
            return "android"
        if protocol == "web":
            return "web"
        raise Cloud115AuthError(
            "rapid upload only supports Android (F1) and Alipay Mini Program (R2) cookies"
        )

    async def _get_upload_userkey(
        self,
        upload_protocol: Literal["web", "android"] = "web",
    ) -> str:
        """懒加载 cookie 上传协议需要的 userkey，只保存在当前客户端实例。"""
        if upload_protocol not in {"web", "android"}:
            raise ValueError("upload_protocol must be 'web' or 'android'")
        if userkey := self._upload_userkeys.get(upload_protocol):
            return userkey
        async with self._upload_userkey_lock:
            if userkey := self._upload_userkeys.get(upload_protocol):
                return userkey
            if upload_protocol == "android":
                url = f"{self._BASE_PROAPI}/android/2.0/user/upload_key"
            else:
                # 网页 Cookie 对 app upload_key 接口会返回 errno=99；网页上传
                # 初始化实际使用的 userkey 由 uploadinfo 接口提供。
                url = f"{self._BASE_PROAPI}/app/uploadinfo"
            payload = await self._request_json("GET", url, retryable=True)
            if not payload.get("state"):
                raise self._map_errno(payload, endpoint=url)
            data = payload.get("data") or {}
            userkey = str(
                payload.get("userkey")
                or payload.get("user_key")
                or (data.get("userkey") if isinstance(data, dict) else "")
                or (data.get("user_key") if isinstance(data, dict) else "")
                or ""
            )
            if not userkey:
                raise Cloud115RequestError(
                    "upload userkey missing from response",
                    method="GET",
                    url=url,
                    detail=str(payload)[:200],
                )
            self._upload_userkeys[upload_protocol] = userkey
            return userkey

    async def _get_upload_app_version(self) -> str:
        """读取官方 Android 当前版本，避免伪造 99.99.99.99 被 WAF 拦截。"""
        if self._upload_app_version:
            return self._upload_app_version
        async with self._upload_app_version_lock:
            if self._upload_app_version:
                return self._upload_app_version
            payload = await self._request_json(
                "GET",
                self._UPLOAD_APP_VERSION_URL,
                # 版本接口是公开接口，不向该域名透传账号 Cookie。
                headers={"Cookie": ""},
                retryable=True,
            )
            data = payload.get("data") or {}
            android = data.get("Android") if isinstance(data, dict) else None
            version = str(android.get("version_code") or "") if isinstance(android, dict) else ""
            if not version:
                raise Cloud115RequestError(
                    "Android upload app version missing from response",
                    method="GET",
                    url=self._UPLOAD_APP_VERSION_URL,
                    detail=str(payload)[:200],
                )
            self._upload_app_version = version
            return version

    async def _upload_init(
        self,
        *,
        filename: str,
        filesize: int,
        filesha1: str,
        pid: str,
        sign_key: str = "",
        sign_val: str = "",
        upload_protocol: Literal["web", "android"] = "web",
    ) -> dict[str, Any]:
        """调用 uplb 初始化接口；这里只提交秒传元数据，不上传文件内容。"""
        url = f"{self._BASE_UPLOAD}/4.0/initupload.php"
        userkey, app_version = await asyncio.gather(
            self._get_upload_userkey(upload_protocol),
            self._get_upload_app_version(),
        )
        payload = {
            "appid": 0,
            "appversion": app_version,
            "fileid": filesha1.upper(),
            "filename": filename,
            "filesize": filesize,
            "target": f"U_1_{pid}",
            "sign_key": sign_key,
            "sign_val": sign_val,
            "topupload": "true",
            "userid": self._user_id,
            "userkey": userkey,
        }
        params, body = make_upload_payload(payload)
        response = await self._request(
            "POST",
            url,
            params=params,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://115.com",
                "Referer": "https://115.com/",
                "User-Agent": (
                    f"Mozilla/5.0 115disk/{app_version} "
                    f"115Browser/{app_version} 115wangpan_android/{app_version}"
                ),
            },
            retryable=False,
        )
        try:
            result = decrypt_upload_response(response.content)
        except Exception as exc:
            raise Cloud115RequestError(
                "invalid encrypted upload init response",
                method="POST",
                url=url,
                detail=str(exc),
            ) from exc
        if not isinstance(result, dict):
            raise Cloud115RequestError(
                "upload init response is not an object",
                method="POST",
                url=url,
            )
        if result.get("state") is False:
            raise self._map_errno(result, endpoint=url)
        return result

    @staticmethod
    def _upload_data(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("state") is False:
            raise Cloud115Error("upload initialization rejected")
        data = response.get("data")
        if not isinstance(data, dict):
            # 旧版 uplb 会直接返回 {status, statuscode, statusmsg}，没有
            # state/data 包装；保留该响应，让上层按 NOT_HIT 处理而不是误判协议崩溃。
            if "status" in response:
                return response
            raise Cloud115RequestError("upload init response missing data")
        return data

    @staticmethod
    def _first_text(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _file_snapshot(path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _hash_file_range(path: Path, sign_check: str) -> str:
        try:
            start_text, end_text = sign_check.split("-", 1)
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise Cloud115RequestError(
                f"invalid upload sign_check: {sign_check!r}"
            ) from exc
        if start < 0 or end < start:
            raise Cloud115RequestError(f"invalid upload byte range: {sign_check!r}")
        digest = hashlib.sha1()
        remaining = end - start + 1
        with path.open("rb") as file:
            file.seek(start)
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise Cloud115RequestError(
                        f"upload byte range exceeds local file: {sign_check!r}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest().upper()
