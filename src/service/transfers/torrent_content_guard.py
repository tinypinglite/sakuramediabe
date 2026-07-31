"""种子内容闸门：提交下载前先看清种子里到底有没有能导入的视频。

判据完全建立在导入侧既有的两条约束上——受支持的视频后缀（``SUPPORTED_VIDEO_EXTENSIONS``）
与最小视频体积（``allowed_min_video_file_size``）——因此"选种阶段拒绝的"恒等于"导入阶段会
丢弃的"，两边不会漂移；将来支持新容器格式或调整体积阈值，闸门自动跟随，不需要在这里维护
任何格式关键词表。

拦住的是两类真实存在、且靠标题和体积都判不出来的资源：
- 蓝光/DVD 原盘：正片是单个 ``.iso``，合格视频数为 0。实测原盘与压制版标题可以逐字相同，
  体积也和 4K 压制重叠（20G 的 mp4 是正片，28G 的 iso 是原盘），只有文件列表能分辨。
- 演员合集包：几十上百部影片塞进一个种子，导入时会把无关影片全灌进同一个番号目录。
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import httpx
import libtorrent as lt
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.config.config import settings

# 单个种子允许包含的合格视频数上限。多分部资源（VR / FC2 的 A、B、C 分卷）实测不超过 4 个，
# 超过这个量的基本是演员合集包——它们的最大文件同样是正常 mp4，只能靠数量识别。
MAX_QUALIFIED_VIDEO_FILES = 6

# 内容确定不合格：换一个候选就能解决，调用方应据此重选。
ERROR_CODE_CONTENT_REJECTED = "download_candidate_content_rejected"
# 拿不到或解析不了种子文件：候选本身可能没问题，是索引器/网络故障，换候选大概率也一样失败。
ERROR_CODE_CONTENT_UNVERIFIABLE = "download_candidate_content_unverifiable"

# Jackett 的 /dl/ 端点要回源到上游站点，偶发超时是常态（实测 30 次里有几次），重试即可恢复。
# 次数与超时压得紧是因为调用方拿到失败后会换下一个候选：单候选的最坏耗时会被换种次数放大，
# 自动下载一部影片最多 MAX_CONTENT_REJECTED_CANDIDATES 轮，不能让每轮都耗满退避。
FETCH_ATTEMPTS = 2
FETCH_TIMEOUT_SECONDS = 20.0


def _redact_url(url: str) -> str:
    """去掉 query 再入日志。

    Jackett 的下载地址形如 ``http://host:9117/dl/<indexer>/?jackett_apikey=<KEY>&path=...``，
    apikey 就在 query 里且紧跟在很短的 host+path 之后，截断长度挡不住它。
    """
    return url.split("?", 1)[0][:120]


def _describe_fetch_error(exc: Exception) -> str:
    """把拉取异常压成不含 URL 的短描述。

    httpx 的异常字符串会内嵌完整请求 URL（连同 apikey），既不能进日志也不能进 ApiError.details
    ——后者会被 API 层原样返回给调用方。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _suffix(file_path: str) -> str:
    name = file_path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def count_qualified_videos(files: Sequence[Tuple[str, int]]) -> int:
    """统计种子里"导入侧真的会收下"的视频文件数。

    与 ``media_source_scanner.scan_source_files`` 的过滤口径逐条对应：后缀命中支持列表，
    且体积不低于最小视频体积（小于阈值的是样本、广告片和下载残片）。
    """
    minimum_size = settings.media.allowed_min_video_file_size
    return sum(
        1
        for file_path, file_size in files
        if _suffix(file_path) in SUPPORTED_VIDEO_EXTENSIONS and file_size >= minimum_size
    )


def fetch_torrent_files(
    torrent_url: str,
    *,
    http_client: httpx.Client | None = None,
) -> List[Tuple[str, int]]:
    """拉取 .torrent 并解出 (文件相对路径, 字节数) 列表。

    只读取 metadata，不加种、不连 DHT、不产生任何下载行为。取不到时抛
    ``ERROR_CODE_CONTENT_UNVERIFIABLE``，由调用方决定是中止还是换候选。
    """
    client = http_client or httpx.Client(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        trust_env=False,
    )
    owns_client = http_client is None
    last_error = ""
    try:
        for attempt in range(FETCH_ATTEMPTS):
            try:
                response = client.get(torrent_url)
                response.raise_for_status()
                torrent_info = lt.torrent_info(response.content)
            except Exception as exc:
                last_error = _describe_fetch_error(exc)
                logger.warning(
                    "Torrent content fetch failed attempt={}/{} url={} detail={}",
                    attempt + 1,
                    FETCH_ATTEMPTS,
                    _redact_url(torrent_url),
                    last_error,
                )
                continue
            file_storage = torrent_info.files()
            return [
                (file_storage.file_path(index), file_storage.file_size(index))
                for index in range(file_storage.num_files())
            ]
    finally:
        if owns_client:
            client.close()

    raise ApiError(
        502,
        ERROR_CODE_CONTENT_UNVERIFIABLE,
        "无法获取种子文件，暂时无法校验资源内容",
        {"detail": last_error},
    )


def assert_candidate_content_importable(
    *,
    title: str,
    torrent_url: str,
    magnet_url: str,
    http_client: httpx.Client | None = None,
) -> None:
    """提交下载前的硬闸门：种子内容不可导入时拒绝提交。

    只有磁力链的候选一律判为不可校验：磁力本身不含文件列表，要拿到只能走 BEP-9 从 swarm
    换 metadata，实测冷门种子经常几分钟都换不到，做不了提交前的同步闸门。

    与下游提交器一致**按内容而非字段名**分流（见 ``QBittorrentClient.add_candidate`` 与
    ``resolve_magnet_from_links``）：索引器会把磁力塞进 torrent_url 字段，照字段名处理会拿
    ``magnet:`` 当 HTTP 地址去 GET，白白重试到超时再报一个与真实原因无关的错误。
    """
    normalized_torrent_url = (torrent_url or "").strip()
    if normalized_torrent_url.lower().startswith("magnet:"):
        normalized_torrent_url = ""
    if not normalized_torrent_url:
        raise ApiError(
            502,
            ERROR_CODE_CONTENT_UNVERIFIABLE,
            "候选只提供了磁力链，无法在下载前校验资源内容",
            {"title": title, "has_magnet": bool((magnet_url or "").strip())},
        )

    files = fetch_torrent_files(normalized_torrent_url, http_client=http_client)
    qualified_count = count_qualified_videos(files)

    if qualified_count == 0:
        logger.info(
            "Download candidate rejected: no importable video title={} total_files={}",
            title,
            len(files),
        )
        raise ApiError(
            422,
            ERROR_CODE_CONTENT_REJECTED,
            "该资源不含可导入的视频文件（常见于蓝光/DVD 原盘）",
            {
                "title": title,
                "total_files": len(files),
                "qualified_videos": 0,
                "biggest_file": max(files, key=lambda item: item[1])[0] if files else "",
            },
        )

    if qualified_count > MAX_QUALIFIED_VIDEO_FILES:
        logger.info(
            "Download candidate rejected: too many videos title={} qualified={} limit={}",
            title,
            qualified_count,
            MAX_QUALIFIED_VIDEO_FILES,
        )
        raise ApiError(
            422,
            ERROR_CODE_CONTENT_REJECTED,
            "该资源包含过多影片，疑似合集包",
            {
                "title": title,
                "total_files": len(files),
                "qualified_videos": qualified_count,
                "limit": MAX_QUALIFIED_VIDEO_FILES,
            },
        )

    logger.info(
        "Download candidate content accepted title={} qualified={} total_files={}",
        title,
        qualified_count,
        len(files),
    )
