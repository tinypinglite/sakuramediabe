from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from src.lib.cloud115.capabilities.base import Cloud115Capability
from src.lib.cloud115.cipher import decrypt_response, encrypt_payload
from src.lib.cloud115.exceptions import (
    Cloud115NotFoundError,
    Cloud115RequestError,
    Cloud115VideoNotReadyError,
)
from src.lib.cloud115.types import DirectUrl, VideoDefinition, VideoInfo, VideoSegment


class PlaybackCapability(Cloud115Capability):
    _BASE_PROAPI = "https://proapi.115.com"
    _BASE_WEBAPI = "https://webapi.115.com"
    _M3U8_ATTR_PATTERN = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))')

    async def download_bytes(
        self,
        pickcode: str,
        *,
        user_agent: str,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> bytes:
        """下载小文件完整内容（字幕等）。封装「拿直链与 GET 必须同 UA」这一易错点。

        - 内部先 get_download_url(pickcode, user_agent) 再用**同一 UA** GET 直链。
        - max_bytes 防御：目标超限直接抛 Cloud115RequestError（本方法只为小文件设计，
          视频请走 /stream 302 或受控 Range 读）。
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        direct = await self.get_download_url(pickcode, user_agent)
        headers = {"User-Agent": user_agent}
        chunks: list[bytes] = []
        received = 0
        try:
            async with self._client.stream("GET", direct.url, headers=headers) as response:
                if response.status_code not in (200, 206):
                    raise Cloud115RequestError(
                        f"http {response.status_code} on GET direct url for pickcode {pickcode}",
                        method="GET",
                        url=direct.url,
                        detail=f"pickcode={pickcode}",
                    )
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise Cloud115RequestError(
                            f"file exceeds max_bytes={max_bytes} for pickcode {pickcode}",
                            method="GET",
                            url=direct.url,
                            detail=f"received>{max_bytes}",
                        )
                    chunks.append(chunk)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise Cloud115RequestError(
                f"network error downloading pickcode {pickcode}: {exc}",
                method="GET",
                url=direct.url,
                detail=str(exc),
            ) from exc
        return b"".join(chunks)

    async def get_download_url(self, pickcode: str, user_agent: str) -> DirectUrl:
        """取 302 直链。

        user_agent 必填：115 会把它绑定进返回 URL 的 f= 指纹，调用方后续 Range GET
        必须一字不差复用同一 UA，否则 403。返回的 DirectUrl.user_agent 就是本参数。
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        if not user_agent:
            raise ValueError("user_agent is required for downurl UA fingerprint binding")

        url = f"{self._BASE_PROAPI}/app/chrome/downurl"
        # payload 编码：{"pickcode": ..., "user_id": ...} -> RSA 加密 -> base64 -> form 里的 data 字段
        payload_body = {"pickcode": pickcode, "user_id": self._user_id}
        data_field = encrypt_payload(payload_body).decode("ascii")

        # downurl 请求本身的 UA 用调用方传入的 UA（服务端据此绑指纹）
        # 另需 Referer，proapi 部分场景不加会 400
        headers = self._base_headers()
        headers["User-Agent"] = user_agent
        headers["Referer"] = "https://115.com/"

        response_json = await self._request_json(
            "POST",
            url,
            data={"data": data_field},
            headers=headers,
            # HTTP 虽为 POST，但该端点只读取并签发直链，不产生远端文件副作用。
            retryable=True,
        )
        if not response_json.get("state"):
            raise self._map_errno(response_json, endpoint=url)

        cipher_b64 = response_json.get("data")
        if not cipher_b64:
            raise Cloud115NotFoundError(
                f"downurl response missing data for pickcode {pickcode}", endpoint=url
            )
        decrypted = decrypt_response(cipher_b64)
        # 解密后是 {"<file_id>": {file_name, file_size, pick_code, sha1, url: {"url": "..."} | 0}}
        if not decrypted:
            raise Cloud115NotFoundError(
                f"downurl decrypted empty for pickcode {pickcode}", endpoint=url
            )
        # 只取第一个（chrome downurl 支持批量，但我们只传一个 pickcode）
        file_id, entry = next(iter(decrypted.items()))
        raw_url = entry.get("url")
        # url == 0 表示条目是目录、或被 115 封禁：从上层视角等同 "拿不到"
        if not isinstance(raw_url, dict):
            raise Cloud115NotFoundError(
                f"downurl refused for pickcode {pickcode} (banned or directory)",
                endpoint=url,
            )
        direct_url = raw_url.get("url", "")
        if not direct_url:
            raise Cloud115NotFoundError(
                f"downurl empty for pickcode {pickcode}", endpoint=url
            )
        return DirectUrl(
            file_id=str(file_id),
            file_name=str(entry.get("file_name", "")),
            file_size=int(entry.get("file_size", 0)),
            sha1=str(entry.get("sha1", "")),
            pickcode=str(entry.get("pick_code", pickcode)),
            url=direct_url,
            user_agent=user_agent,
            expires_at=self._parse_expires_at(direct_url),
        )

    async def get_video_info(self, pickcode: str) -> VideoInfo:
        """获取视频元数据与 master m3u8 中的清晰度列表（VIP 专属）。"""
        if not pickcode:
            raise ValueError("pickcode is required")
        url = f"{self._BASE_WEBAPI}/files/video"
        payload = await self._request_json(
            "GET",
            url,
            params={"pickcode": pickcode},
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

        # 先判断转码状态，避免把尚未生成 video_url 的视频误判成非视频文件。
        raw_status = payload.get("file_status")
        if raw_status is not None:
            try:
                file_status = int(raw_status)
            except (TypeError, ValueError):
                file_status = 1
            if file_status != 1:
                raise Cloud115VideoNotReadyError(
                    f"video not ready for pickcode {pickcode} "
                    f"(file_status={file_status})",
                    file_status=file_status,
                    endpoint=url,
                )

        master_m3u8_url = str(payload.get("video_url", "") or "")
        if not master_m3u8_url:
            raise Cloud115NotFoundError(
                f"video_url missing for pickcode {pickcode} (not a video)",
                endpoint=url,
            )

        master_text = await self._get_text(master_m3u8_url)
        definitions = self._parse_master_m3u8(
            master_text,
            base_url=master_m3u8_url,
        )
        return VideoInfo(
            pickcode=pickcode,
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            thumb_url=str(payload.get("thumb_url", "") or ""),
            master_m3u8_url=master_m3u8_url,
            definitions=definitions,
        )

    async def get_video_segments(
        self,
        pickcode: str,
        *,
        prefer_bandwidth: int | None = None,
    ) -> list[VideoSegment]:
        """获取指定码率或最高码率 variant 的 HLS TS 分段列表。"""
        info = await self.get_video_info(pickcode)
        if not info.definitions:
            raise Cloud115NotFoundError(
                f"no video definitions available for pickcode {pickcode}",
                endpoint=info.master_m3u8_url,
            )
        variant = self._pick_variant(info.definitions, prefer_bandwidth)
        return await self.get_video_segments_for_definition(variant)

    async def get_video_segments_for_definition(
        self,
        definition: VideoDefinition,
    ) -> list[VideoSegment]:
        """读取已解析清晰度分支，避免上层重复请求 master playlist。"""
        if not definition.m3u8_url:
            raise ValueError("definition.m3u8_url is required")
        variant_text = await self._get_text(definition.m3u8_url)
        return self._parse_variant_m3u8(
            variant_text,
            base_url=definition.m3u8_url,
        )

    @staticmethod
    def _parse_expires_at(direct_url: str) -> int:
        """从直链的 t=<unix_ts> query 参数解出过期时间；缺失或非法返回 -1。"""
        try:
            query = urlsplit(direct_url).query
            for key, value in parse_qsl(query):
                if key == "t" and value.isdigit():
                    return int(value)
        except Exception:
            pass
        return -1

    @classmethod
    def _parse_master_m3u8(
        cls,
        text: str,
        *,
        base_url: str,
    ) -> list[VideoDefinition]:
        """解析 master playlist，并把相对 variant 地址转换为绝对地址。"""
        definitions: list[VideoDefinition] = []
        pending_attrs: dict[str, str] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                attrs_text = line[len("#EXT-X-STREAM-INF:") :]
                pending_attrs = {}
                for match in cls._M3U8_ATTR_PATTERN.finditer(attrs_text):
                    value = (
                        match.group(2)
                        if match.group(2) is not None
                        else match.group(3)
                    )
                    pending_attrs[match.group(1)] = value
                continue
            if line.startswith("#"):
                continue

            attrs = pending_attrs or {}
            pending_attrs = None
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0") or "0")
            except ValueError:
                bandwidth = 0
            definitions.append(
                VideoDefinition(
                    bandwidth=bandwidth,
                    resolution=attrs.get("RESOLUTION", ""),
                    label=attrs.get("NAME", ""),
                    m3u8_url=urljoin(base_url, line),
                )
            )
        return definitions

    @classmethod
    def _parse_variant_m3u8(
        cls,
        text: str,
        *,
        base_url: str,
    ) -> list[VideoSegment]:
        """解析 variant playlist，并把相对 TS 地址转换为绝对地址。"""
        segments: list[VideoSegment] = []
        pending_duration: float | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                duration_text = line[len("#EXTINF:") :].split(",", 1)[0]
                try:
                    pending_duration = float(duration_text)
                except ValueError:
                    pending_duration = None
                continue
            if line.startswith("#"):
                continue

            duration = pending_duration if pending_duration is not None else 0.0
            pending_duration = None
            segments.append(
                VideoSegment(
                    index=len(segments),
                    url=urljoin(base_url, line),
                    duration_seconds=duration,
                )
            )
        return segments

    @staticmethod
    def _pick_variant(
        definitions: list[VideoDefinition],
        prefer_bandwidth: int | None,
    ) -> VideoDefinition:
        """精确匹配偏好码率；未命中时选择最高码率。"""
        if prefer_bandwidth is not None:
            exact = next(
                (
                    definition
                    for definition in definitions
                    if definition.bandwidth == prefer_bandwidth
                ),
                None,
            )
            if exact is not None:
                return exact
        return max(definitions, key=lambda definition: definition.bandwidth)
