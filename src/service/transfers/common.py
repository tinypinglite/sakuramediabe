import base64
import binascii
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence, Set
from urllib.parse import urlparse

from peewee import fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import require_record, resolve_sort, validate_page as _validate_page
from src.config.config import settings
from src.model import (
    DownloadClient,
    DownloadTask,
    Indexer,
    IndexerDownloadClient,
    MediaLibrary,
    Movie,
)
from src.model.enums import DownloadClientKind, MediaLibraryBackend
# 导入状态取值统一收口到 media_import_status 模块；此处再导出，兼容历史引用路径。
from src.common.media_import_status import ALLOWED_IMPORT_STATUSES

BTIH_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
BTIH_BASE32_PATTERN = re.compile(r"^[A-Z2-7a-z]{32}$")


def canonicalize_btih(value: str) -> str:
    """把 hex/Base32 BTIH 严格规范化为 40 位小写 hex。

    种子在本系统里的**唯一身份**。放在 transfers 公共模块而不是某个下载器模块里：它是纯字符串
    处理，与 115 / qb / 索引器都无关，而选种、离线对账、任务删除、索引器候选四条链路都要用它，
    且必须用同一个实现——不同写法的同一个 hash（大小写、Base32）必须收敛到同一个字符串，否则
    「这个种子是不是同一个」在不同链路上会给出不同答案。
    """
    normalized = (value or "").strip()
    if BTIH_HEX_PATTERN.fullmatch(normalized):
        return normalized.lower()
    if BTIH_BASE32_PATTERN.fullmatch(normalized):
        try:
            decoded = base64.b32decode(normalized.upper(), casefold=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("BTIH Base32 解码失败") from exc
        if len(decoded) == 20:
            return decoded.hex()
    raise ValueError("BTIH 必须是 40 位 hex 或 32 位 Base32")

ALLOWED_DOWNLOAD_STATES = {
    "downloading",
    "completed",
    "seeding",
    "paused",
    "failed",
    "stalled",
    "checking",
    "queued",
    # cloud115 离线任务超时后的本地放弃态：不再对账、不再推进度；115 侧任务保留。**粘性终态**，
    # 一旦置上不会自己流回（删任务时据它决定动不动 115 远端）。
    "abandoned",
    # qB 侧的死种态：stalledDL 且 qB 报告的 last_activity 已超过 qbittorrent_stalled_abandon_days。
    # 与 abandoned 相反，它**不粘**——每轮对账都按 qB 的实时 last_activity 重算，peer 回来会自动
    # 流回 stalled/downloading。两者语义不同，故不复用同一个取值。
    "stalled_dead",
}
# 下载已完成的状态集合：completed（下完但不做种）与 seeding（做种中）在业务上都算文件已写定，
# 可触发自动导入 / 允许手动导入。所有需要判"任务是否已完成下载"的地方走 is_download_complete。
DOWNLOAD_COMPLETE_STATES = {"completed", "seeding"}
# 死态集合：本地已确知不会再有进展。判死不删记录——DownloadTask 行本身就是"这部影片试过哪个种子"
# 的台账，选种黑名单直接读它，删了黑名单就没了数据来源。
#
# 判死全部发生在**对账时**（写进 download_state），查询侧只做一次集合判定，因此这里不需要任何时间
# 参数。这一点是刻意的：判定依赖 qB 的 last_activity，而 qB 联系不上时我们不该替它宣布种子死亡，
# 对账不跑 = 状态冻结，正是想要的行为。
DOWNLOAD_DEAD_STATES = {"failed", "abandoned", "stalled_dead"}
DOWNLOAD_STALLED_STATE = "stalled"
DOWNLOAD_STALLED_DEAD_STATE = "stalled_dead"
# paused 是用户在 qB 里的显式意图，永远不判死。
TASK_SORT_FIELDS = {
    "created_at:desc": (DownloadTask.created_at.desc(), DownloadTask.id.desc()),
    "created_at:asc": (DownloadTask.created_at.asc(), DownloadTask.id.asc()),
    "updated_at:desc": (DownloadTask.updated_at.desc(), DownloadTask.id.desc()),
    "updated_at:asc": (DownloadTask.updated_at.asc(), DownloadTask.id.asc()),
    "progress:desc": (DownloadTask.progress.desc(), DownloadTask.id.desc()),
    "progress:asc": (DownloadTask.progress.asc(), DownloadTask.id.asc()),
}
SYSTEM_QB_TAG = "sakuramedia"
CLIENT_QB_TAG_PREFIX = "client:"
# qBittorrent 用 8_640_000（100 天）代表 ETA 未知/无穷大，直接透传会显示成 100 天倒计时。
QB_ETA_INFINITY = 8_640_000


def parse_qb_tags(tags: object) -> Set[str]:
    """把 qB 的 tags 字段（逗号分隔字符串 / None / 属性对象）统一切成集合，去掉空白项。"""
    return {item.strip() for item in str(tags or "").split(",") if item.strip()}


def is_qb_managed_torrent(tags: object, client_id: int) -> bool:
    """判定 qB 种子是否由本系统当前下载客户端管理：必须同时含 sakuramedia 与 client:<id> 标签。"""
    parsed = parse_qb_tags(tags)
    return SYSTEM_QB_TAG in parsed and f"{CLIENT_QB_TAG_PREFIX}{client_id}" in parsed


def map_download_state(raw_state: str) -> str:
    """把 qBittorrent 原始 state 归一化为本系统的下载状态枚举。"""
    normalized = (raw_state or "").strip()
    # qBittorrent 5.x 把 pausedUP/pausedDL 改名为 stoppedUP/stoppedDL，两套名称都要兼容
    # 只有进入做种/完成态（下列 *UP 状态）才算下载完成。qB 把所有 piece 下完后 progress 即为
    # 1.0，但随后还要经历 moving（把文件从下载目录搬到完成目录，跨文件系统时实为逐步复制）阶段，
    # 此时目标文件仍在被写入；下载尾段的 downloading/checkingDL 同样 progress 偏高而内容未定。
    # 因此绝不能仅凭 progress>=1 判完成，否则自动导入会读到未写完的文件，内容指纹抽样到不稳定
    # 字节导致去重失效，把同一份内容反复导入成多条媒体记录。qB 只有在搬运与校验都结束后才会进入
    # *UP 状态，这才是文件写定的可靠信号（与 qbittorrent-api 的 is_complete 判定一致）。
    # 归一化拆分：真正在上传/做种的 *UP 落到 seeding，下游 is_download_complete 会把 seeding
    # 与 completed 视为等价"已完成"参与自动导入判定。前端因此可以单独把做种态用不同 badge 展示。
    # pausedUP / stoppedUP 的数据已经完整，只是停止做种，应归到 completed；否则一个已下载完
    # 且停止上传的任务会一直显示"已暂停"，也无法进入后续自动导入流程。
    if normalized in {"uploading", "stalledUP", "queuedUP", "forcedUP"}:
        return "seeding"
    if normalized in {"pausedUP", "stoppedUP"}:
        return "completed"
    if normalized in {"pausedDL", "stoppedDL"}:
        return "paused"
    if normalized in {"error", "missingFiles"}:
        return "failed"
    # 只收 stalledDL。stalledUP（做种中无对端）已在上面归到 seeding，绝不能同时列在这里：
    # 死种判定把 stalled 当作"下载中无源"来判，一旦做种任务落进这个状态会被判死并加入选种黑名单。
    if normalized == "stalledDL":
        return "stalled"
    if normalized in {"checkingDL", "checkingUP", "checkingResumeData"}:
        return "checking"
    if normalized in {"queuedDL", "queuedUP"}:
        return "queued"
    if normalized in {"downloading", "metaDL", "forcedDL", "allocating"}:
        return "downloading"
    if normalized.lower() in ALLOWED_DOWNLOAD_STATES:
        return normalized.lower()
    return "queued"


def is_download_complete(state: str) -> bool:
    """判断下载状态是否已进入"文件写定"阶段（含做种）。"""
    return state in DOWNLOAD_COMPLETE_STATES


def resolve_qbittorrent_download_state(raw_state: str, last_activity: int | None) -> str:
    """把 qB 原始 state 归一化，并把"躺太久的 stalledDL"升级成死种态。

    死种判定用 qB 自己的 ``last_activity``（官方定义："Last time (Unix Epoch) when a chunk was
    downloaded/uploaded"），不再自己维护一个"最后有进展时刻"的代理列。qB 对从未有过活动的种子返回
    的是**添加时刻**而非哨兵值（见 serialize_torrent.cpp 的 getLastActivityTime），所以"加进来 N
    天一个 chunk 都没收到"也能被正确判死，不需要额外分支。

    只在拿到可信的 last_activity（正整数且不在未来）时才判死：宁可漏判也不误判——误判的代价是给
    还活着的种子拉黑并重复提交同一部影片。
    """
    from src.common.runtime_time import utc_now_for_db

    normalized = map_download_state(raw_state)
    if normalized != DOWNLOAD_STALLED_STATE:
        return normalized
    if not isinstance(last_activity, int) or isinstance(last_activity, bool):
        return normalized

    now = utc_now_for_db()
    try:
        activity_at = datetime.utcfromtimestamp(last_activity)
    except (OverflowError, OSError, ValueError):
        return normalized
    if last_activity <= 0 or activity_at > now:
        return normalized
    if now - activity_at >= timedelta(days=settings.downloads.qbittorrent_stalled_abandon_days):
        return DOWNLOAD_STALLED_DEAD_STATE
    return normalized


def download_task_dead_expression():
    """DownloadTask 是否已判死的 peewee 条件表达式，供选种黑名单与活跃任务判定复用。

    纯集合判定：判死已经在对账时完成并落进 download_state（该列有索引）。
    """
    return DownloadTask.download_state.in_(tuple(sorted(DOWNLOAD_DEAD_STATES)))


def download_task_movie_match_expression():
    """DownloadTask 与 Movie 的番号关联条件。

    **两侧都必须是裸列**：movie.movie_number 存 provider 规范原样，download_task.movie_number
    由提交链路拷贝同一列（对账重建行只填空不覆写，见 DownloadSyncService），两列直接可比。
    任何一侧套上 UPPER(TRIM()) 都会让该列索引失效，该表达式所在的相关子查询退化为逐行全表顺扫。
    """
    return DownloadTask.movie == Movie.movie_number


def active_download_task_exists_expression():
    """影片是否还有"活着的"下载任务。

    判定的是活跃而非存在：failed / abandoned / stalled_dead 的任务留在库里当台账，但不再阻塞
    重新查资源——过去按"存在任何 DownloadTask"判定，死种会让那部影片永久不再被查。
    completed 但导入失败的任务仍算活跃：文件已经在盘上，该修的是导入而不是重下。
    """
    active_tasks = DownloadTask.select(DownloadTask.id).where(
        download_task_movie_match_expression() & ~download_task_dead_expression()
    )
    return fn.EXISTS(active_tasks)


def require_client(client_id: int) -> DownloadClient:
    return require_record(
        DownloadClient, DownloadClient.id == client_id,
        error_code="download_client_not_found",
        error_message="Download client not found",
        error_details={"client_id": client_id},
    )


def require_media_library(library_id: int) -> MediaLibrary:
    return require_record(
        MediaLibrary, MediaLibrary.id == library_id,
        error_code="media_library_not_found",
        error_message="Media library not found",
        error_details={"library_id": library_id},
    )


def require_local_media_library(library_id: int) -> MediaLibrary:
    library = require_media_library(library_id)
    if library.backend != MediaLibraryBackend.LOCAL.value:
        raise ApiError(
            422,
            "media_library_backend_mismatch",
            "该操作要求 local 媒体库",
            {
                "library_id": library_id,
                "expected_backend": MediaLibraryBackend.LOCAL.value,
                "actual_backend": library.backend,
            },
        )
    root_path = (library.backend_config or {}).get("root_path")
    if not isinstance(root_path, str) or not root_path:
        raise ApiError(
            422,
            "invalid_media_library_backend_config",
            "local 媒体库缺少 root_path",
            {"library_id": library_id},
        )
    return library


def require_cloud115_media_library(library_id: int) -> MediaLibrary:
    library = require_media_library(library_id)
    if library.backend != MediaLibraryBackend.CLOUD115.value:
        raise ApiError(
            422,
            "media_library_backend_mismatch",
            "该操作要求 cloud115 媒体库",
            {
                "library_id": library_id,
                "expected_backend": MediaLibraryBackend.CLOUD115.value,
                "actual_backend": library.backend,
            },
        )
    config = library.backend_config or {}
    if not config.get("cookies") or not config.get("root_cid"):
        raise ApiError(
            422,
            "invalid_media_library_backend_config",
            "cloud115 媒体库缺少 cookies/root_cid",
            {"library_id": library_id},
        )
    return library


def validate_download_client_kind(value: str) -> str:
    normalized = (value or "").strip().lower()
    try:
        return DownloadClientKind(normalized).value
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_download_client_kind",
            "Unsupported download client kind",
            {"kind": value},
        ) from exc


def require_indexer(indexer_name: str) -> Indexer:
    normalized = indexer_name.strip()
    if not normalized:
        raise ApiError(
            422,
            "download_request_indexer_not_found",
            "Indexer not found",
            {"indexer_name": indexer_name},
        )
    indexer = Indexer.get_or_none(Indexer.name == normalized)
    if indexer is None:
        raise ApiError(
            422,
            "download_request_indexer_not_found",
            "Indexer not found",
            {"indexer_name": normalized},
        )
    return indexer


def list_indexer_clients(indexer: Indexer) -> list[DownloadClient]:
    """按绑定顺序（中间表 id 升序）取出索引器绑定的全部下载器。"""
    return [
        link.download_client
        for link in (
            IndexerDownloadClient.select(IndexerDownloadClient, DownloadClient)
            .join(DownloadClient)
            .where(IndexerDownloadClient.indexer == indexer.id)
            .order_by(IndexerDownloadClient.id.asc())
        )
    ]


def resolve_preferred_client(clients: Sequence[DownloadClient]) -> DownloadClient:
    """按全局 kind 偏好从候选下载器中挑一个。

    偏好列表只决定挑选顺序，不做白名单：列表外的 kind 排最后，同 kind 内保持绑定顺序。
    选中的下载器后续执行失败时由调用方直接报错，不自动换下一个（用户已明确不降级）。
    """
    if not clients:
        raise ApiError(
            422,
            "download_request_client_resolution_failed",
            "Indexer has no bound download clients",
        )
    preferred_kinds = settings.downloads.preferred_client_kinds
    for kind in preferred_kinds:
        for client in clients:
            if client.kind == kind:
                return client
    return clients[0]


def require_task(task_id: int) -> DownloadTask:
    return require_record(
        DownloadTask, DownloadTask.id == task_id,
        error_code="download_task_not_found",
        error_message="Download task not found",
        error_details={"task_id": task_id},
    )


def validate_non_empty(value: str, code: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApiError(422, code, message)
    return normalized


def validate_base_url(value: str) -> str:
    normalized = validate_non_empty(
        value,
        "invalid_download_client_base_url",
        "Download client base URL cannot be empty",
    )
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError(
            422,
            "invalid_download_client_base_url",
            "Download client base URL must use http or https",
        )
    return normalized


def validate_absolute_path(value: str, *, field_name: str) -> str:
    normalized = validate_non_empty(
        value,
        f"invalid_download_client_{field_name}",
        f"{field_name} cannot be empty",
    )
    if not Path(normalized).is_absolute():
        raise ApiError(
            422,
            f"invalid_download_client_{field_name}",
            f"{field_name} must be an absolute path",
        )
    return normalized


def validate_media_library_id(library_id: int) -> int:
    if library_id <= 0:
        raise ApiError(
            422,
            "invalid_download_client_media_library_id",
            "Media library ID must be a positive integer",
        )
    return library_id


def ensure_name_available(name: str, exclude_client_id: Optional[int] = None) -> None:
    query = DownloadClient.select().where(DownloadClient.name == name)
    if exclude_client_id is not None:
        query = query.where(DownloadClient.id != exclude_client_id)
    if query.exists():
        raise ApiError(
            409,
            "download_client_name_conflict",
            "Download client name already exists",
            {"name": name},
        )


def normalize_state_filter(
    value: Optional[str],
    *,
    field_name: str,
    allowed_values: Set[str],
) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in allowed_values:
        raise ApiError(
            422,
            "invalid_download_task_filter",
            f"Invalid {field_name}",
            {field_name: value},
        )
    return normalized


def resolve_task_sort(value: Optional[str]) -> Sequence:
    return resolve_sort(
        value, TASK_SORT_FIELDS,
        default_key="created_at:desc", error_code="invalid_download_task_filter",
    )


def validate_page(page: int, page_size: int) -> None:
    _validate_page(page, page_size, error_code="invalid_download_task_filter")


def validate_task_ids(task_ids: Optional[str]) -> list[int]:
    if task_ids is None or not task_ids.strip():
        raise ApiError(
            422,
            "invalid_download_task_ids",
            "task_ids is required",
        )

    values = []
    for raw_part in task_ids.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ApiError(
                422,
                "invalid_download_task_ids",
                "task_ids must be a comma-separated list of positive integers",
                {"task_ids": task_ids},
            )
        values.append(int(part))

    if not values:
        raise ApiError(
            422,
            "invalid_download_task_ids",
            "task_ids must be a comma-separated list of positive integers",
            {"task_ids": task_ids},
        )
    return sorted(set(values))


def build_task_movie_filter(movie_number: str):
    # 入参来自影片页/任务页的规范番号，movie 列由提交链路拷贝 Movie.movie_number，
    # 两侧直接裸列精确比较；套函数会废掉该列索引。
    return DownloadTask.movie == movie_number.strip()


# 番号子目录只保留字母数字与连字符、下划线、点，其余字符统一替换成下划线，杜绝路径穿越与非法目录名。
_UNSAFE_SUBDIR_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_movie_subdir_name(movie_number: str) -> str:
    """净化番号为安全目录名：替换非法字符，再去掉首尾的点/下划线/连字符，防止隐藏目录或空段。

    qb 的本地保存子目录与 cloud115 的下载缓冲子目录共用同一套净化规则。
    """
    safe_number = _UNSAFE_SUBDIR_CHARS.sub("_", movie_number.strip()).strip("._-")
    if not safe_number:
        raise ApiError(
            422,
            "invalid_download_request_movie_number",
            "movie_number 无法生成有效的保存目录",
            {"movie_number": movie_number},
        )
    return safe_number


def build_movie_save_path(client_save_path: str, movie_number: str) -> str:
    """按番号生成 qB 端的种子保存子目录，使每个种子独立落盘，避免内容平铺到下载根目录。"""
    return f"{client_save_path.rstrip('/')}/{safe_movie_subdir_name(movie_number)}"


def map_remote_path(client: DownloadClient, remote_path: str) -> str:
    validate_non_empty(
        remote_path,
        "invalid_download_task_save_path",
        "Download task save path cannot be empty",
    )
    # 统一去掉尾部斜杠再比较：qB 报告的路径会 strip 掉尾斜杠，而配置里的 client_save_path 可能带尾斜杠，
    # 否则刚加完磁链（content_path 为空、_to_dict 回退用 save_path）时会因斜杠差异把正确路径误判为不匹配。
    normalized_remote = remote_path.strip().rstrip("/")
    client_save_path = client.client_save_path.rstrip("/")
    local_root_path = client.local_root_path.rstrip("/")
    if normalized_remote == client_save_path:
        return client.local_root_path
    prefix = f"{client_save_path}/"
    if normalized_remote.startswith(prefix):
        suffix = normalized_remote[len(prefix):]
        return f"{local_root_path}/{suffix}"
    raise ApiError(
        422,
        "invalid_download_client_path_mapping",
        "Download client path mapping does not match qBittorrent save path",
        {
            "client_id": client.id,
            "remote_path": normalized_remote,
            "client_save_path": client.client_save_path,
        },
    )
