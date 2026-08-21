"""种子内容闸门：提交下载前先看清种子里到底有没有能导入的视频。

闸门是"尽力而为的预检"而不是硬闸门：**候选能拿到 .torrent 文件列表时才校验**，
只有磁力链的候选直接放行、内容校验推迟到下载完成后的导入阶段（磁力本身不含文件列表，
要拿到只能走 BEP-9 从 swarm 换 metadata，冷门种子实测几十秒到几分钟都换不到，做不了
提交前的同步闸门）。放行的代价是原盘/合集包会真实下载下来：原盘导入时无合格视频会
明确失败（TaskRun 摘要含失败计数且 `DownloadTask.import_status=failed`），合集包会混入媒体库，
由用户删任务清理。

有文件列表时的判据建立在导入侧既有的约束上——受支持的视频后缀
（``SUPPORTED_VIDEO_EXTENSIONS``）与最小视频体积（``allowed_min_video_file_size``）——
因此"选种阶段拒绝的"恒等于"导入阶段会丢弃的"，两边不会漂移；将来支持新容器格式或调整
体积阈值，闸门自动跟随，不需要在这里维护任何格式关键词表。合集判定额外复用导入侧的
番号解析函数（见 ``count_distinct_movie_numbers``）。

拦住的是三类真实存在、且靠标题和体积都判不出来的资源：
- 蓝光/DVD 原盘：正片是单个 ``.iso``，合格视频数为 0。实测原盘与压制版标题可以逐字相同，
  体积也和 4K 压制重叠（20G 的 mp4 是正片，28G 的 iso 是原盘），只有文件列表能分辨。
- 演员合集包：几十上百部影片塞进一个种子，文件名能解析出多个不同番号，靠番号数识别；
  单部影片的多分卷（VR / FC2 的 A、B、C）解析出的是同一个番号，不受影响。

另外在提交阶段补一道**番号一致性**判据：种子内容能解析出番号、但与请求番号不一致时
（例如搜索 JOB-033 拿到内容为 CJOB-033 的种子），导入侧必然会把文件归到别的影片，
必须提交前拒绝；标题级过滤在搜索阶段已经做过一遍，这里是权威兜底。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
import libtorrent as lt
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS, video_suffix
from src.config.config import settings
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.imports.source_scanner import (
    parse_movie_number_from_scan_path,
)
from src.service.transfers.shared.common import canonicalize_btih

# 内容确定不合格：换一个候选就能解决，调用方应据此重选。
ERROR_CODE_CONTENT_REJECTED = "download_candidate_content_rejected"
# 拿不到或解析不了种子文件：候选本身可能没问题，是索引器/网络故障，换候选大概率也一样失败。
ERROR_CODE_CONTENT_UNVERIFIABLE = "download_candidate_content_unverifiable"

# Torznab 聚合器（如 Jackett）的 /dl/ 下载端点要回源到上游站点，偶发超时是常态（实测 30 次里有几次），重试即可恢复。
# 次数与超时压得紧是因为调用方拿到失败后会换下一个候选：单候选的最坏耗时会被换种次数放大，
# 自动下载一部影片最多 MAX_REJECTED_CANDIDATES 轮，不能让每轮都耗满退避。
FETCH_ATTEMPTS = 2
FETCH_TIMEOUT_SECONDS = 20.0


def _redact_url(url: str) -> str:
    """去掉 query 再入日志。

    Torznab 服务返回的下载地址通常自带鉴权参数（Jackett 形如
    ``http://host:9117/dl/<indexer>/?jackett_apikey=<KEY>&path=...``），
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


def count_qualified_videos(files: Sequence[tuple[str, int]]) -> int:
    """统计种子里"导入侧真的会收下"的视频文件数。

    与 ``media_source_scanner.scan_source_files`` 的过滤口径逐条对应：后缀命中支持列表，
    且体积不低于最小视频体积（小于阈值的是样本、广告片和下载残片）。
    """
    minimum_size = settings.media.allowed_min_video_file_size
    return sum(
        1
        for file_path, file_size in files
        if video_suffix(file_path.rsplit("/", 1)[-1]) in SUPPORTED_VIDEO_EXTENSIONS and file_size >= minimum_size
    )


def collect_distinct_movie_numbers(files: Sequence[tuple[str, int]]) -> set[str]:
    """收集合格视频解析出的**不同番号**集合，作为合集判定与番号一致性校验依据。

    与导入侧扫描共用 ``parse_movie_number_from_scan_path``（只看父目录 + 文件名最后两段），
    并对种子内相对路径垫一个虚拟根段，使解析口径与落盘后的绝对路径完全对齐：
    - 多文件种子（含无根目录、番号只在目录名的 2 段路径）解析结论与导入侧逐文件一致；
    - 单文件种子文件名解析不出番号时这里判 0（放行），导入侧却能靠父目录番号兜底成功，放行是安全的。

    去重直接按解析输出原串（解析结果已 upper）比较，**不做** ``normalize_movie_number`` 折叠：
    一本道 ``072625_001`` 与加勒比 ``072625-001`` 是同字符串形态的两部不同影片，折叠会把它们误并成一部。
    """
    minimum_size = settings.media.allowed_min_video_file_size
    distinct_numbers: set[str] = set()
    for file_path, file_size in files:
        if video_suffix(file_path.rsplit("/", 1)[-1]) not in SUPPORTED_VIDEO_EXTENSIONS or file_size < minimum_size:
            continue
        # 给种子内相对路径垫一个虚拟根段，与落盘后的绝对路径结构对齐：导入侧对
        # <save_path>/<种子内路径> 解析时路径段数恒 >= 3，走父目录 + 文件名分支；不垫层时
        # 无根目录的 2 段路径（如 STARS-001/part.mkv）会退化成只看文件名，番号只在目录名时
        # 闸门漏拦截合集包。垫一层不影响单文件种子：仍只看文件名，解析不出则 0 番号放行，
        # 导入侧靠番号父目录兜底，放行是安全的。
        movie_number = parse_movie_number_from_scan_path(f"root/{file_path}")
        if movie_number:
            distinct_numbers.add(movie_number)
    return distinct_numbers


def count_distinct_movie_numbers(files: Sequence[tuple[str, int]]) -> int:
    """统计合格视频解析出的不同番号数，作为合集判定依据。"""
    return len(collect_distinct_movie_numbers(files))


def content_movie_numbers_match(movie_number: str, content_movie_numbers: set[str]) -> bool:
    """内容番号是否命中请求番号（严格原串大写比较，不做分隔符折叠）。

    解析不出番号（集合为空）时放行：单文件种子可能靠落盘后的父目录番号兜底，
    与导入侧路径解析口径一致。``_``/``-`` 形态代表不同影片（一本道 / 加勒比），
    因此不能走 ``normalize_movie_number`` 折叠后再比较。
    """
    requested = (movie_number or "").strip().upper()
    if not requested or not content_movie_numbers:
        return True
    return requested in {number.strip().upper() for number in content_movie_numbers}


@dataclass(frozen=True)
class TorrentInspection:
    """一次 .torrent 解析的产物：种子身份 + 文件清单。"""

    info_hash: str
    files: list[tuple[str, int]]


def fetch_torrent_files(
    torrent_url: str,
    *,
    http_client: httpx.Client | None = None,
) -> TorrentInspection:
    """拉取 .torrent 并解出种子 info_hash 与 (文件相对路径, 字节数) 列表。

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
            info_hash = str(torrent_info.info_hash()).lower()
            file_storage = torrent_info.files()
            return TorrentInspection(
                info_hash=info_hash,
                files=[
                    (file_storage.file_path(index), file_storage.file_size(index))
                    for index in range(file_storage.num_files())
                ],
            )
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
    movie_number: str,
    title: str,
    torrent_url: str,
    magnet_url: str,
    http_client: httpx.Client | None = None,
) -> str:
    """提交下载前的硬闸门：种子内容不可导入时拒绝提交。

    ``movie_number`` 是请求方期望的影片番号：内容能解析出番号但集合不包含它时，
    说明提交后导入侧也会把文件归到别的影片，属于确定性错配，必须拒绝。
    内容通过时返回从 .torrent 解析出的 canonical info_hash（torrent-only 候选的身份
    只能在提交前这一拉里廉价确定，调用方用它做死种黑名单比对）。

    **只有磁力链的候选直接放行**：磁力本身不含文件列表，要拿到只能走 BEP-9 从 swarm
    换 metadata，冷门种子实测几十秒到几分钟都换不到，做不了提交前的同步闸门。
    放行时内容校验推迟到下载完成后的导入阶段（原盘无合格视频会明确导入失败），
    种子身份改从磁力 btih 解析——btih 与 .torrent 的 info_hash 是同一值，死种黑名单
    语义不变。

    与下游提交器一致**按内容而非字段名**分流（见 ``QBittorrentClient.add_candidate`` 与
    ``resolve_magnet_from_links``）：索引器会把磁力塞进 torrent_url 字段，照字段名处理会拿
    ``magnet:`` 当 HTTP 地址去 GET，白白重试到超时再报一个与真实原因无关的错误。
    """
    normalized_torrent_url = (torrent_url or "").strip()
    magnet_link = (magnet_url or "").strip()
    if normalized_torrent_url.lower().startswith("magnet:"):
        magnet_link = magnet_link or normalized_torrent_url
        normalized_torrent_url = ""
    if not normalized_torrent_url:
        # 纯磁力候选：内容不可预检，直接放行；身份从磁力 btih 提取。
        # 磁力链必有 btih，解析不出来就不是合法磁力，下游（115 / qB）同样提交不了，拒绝合理。
        try:
            raw_hash = QBittorrentClient.parse_hash_from_magnet(magnet_link)
        except QBittorrentClientError as exc:
            raise ApiError(
                422,
                "invalid_download_request_candidate",
                "候选磁力链缺少可解析的 btih",
                {"title": title},
            ) from exc
        try:
            return canonicalize_btih(raw_hash)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_download_request_candidate",
                "候选磁力链 btih 无法解析",
                {"title": title, "detail": str(exc)},
            ) from exc

    inspection = fetch_torrent_files(normalized_torrent_url, http_client=http_client)
    files = inspection.files
    qualified_count = count_qualified_videos(files)
    distinct_numbers = collect_distinct_movie_numbers(files)

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

    if distinct_numbers and not content_movie_numbers_match(movie_number, distinct_numbers):
        logger.info(
            "Download candidate rejected: content movie number mismatch title={} requested={} content={}",
            title,
            movie_number,
            sorted(distinct_numbers),
        )
        raise ApiError(
            422,
            ERROR_CODE_CONTENT_REJECTED,
            "资源内容番号与目标不一致",
            {
                "title": title,
                "requested_movie_number": movie_number,
                "content_movie_numbers": sorted(distinct_numbers),
            },
        )

    if len(distinct_numbers) > 1:
        logger.info(
            "Download candidate rejected: too many distinct movie numbers title={} requested={} qualified={} distinct={} total_files={}",
            title,
            movie_number,
            qualified_count,
            sorted(distinct_numbers),
            len(files),
        )
        raise ApiError(
            422,
            ERROR_CODE_CONTENT_REJECTED,
            "该资源包含多部影片，疑似合集包",
            {
                "title": title,
                "requested_movie_number": movie_number,
                "total_files": len(files),
                "qualified_videos": qualified_count,
                "distinct_movie_numbers": sorted(distinct_numbers),
            },
        )

    if distinct_numbers == 0:
        logger.warning(
            "Download candidate has no parseable movie number movie_number={} title={} qualified={} total_files={} "
            "import may fail after download",
            movie_number,
            title,
            qualified_count,
            len(files),
        )
    else:
        logger.info(
            "Download candidate content accepted movie_number={} title={} qualified={} content={} total_files={}",
            movie_number,
            title,
            qualified_count,
            sorted(distinct_numbers),
            len(files),
        )
    return inspection.info_hash
