"""115 SDK 对外数据类型。

全部为 frozen dataclass：DirEntry / FileMeta / DirMeta / DirectUrl 等。
故意与 Pydantic schema 解耦：这是底层 SDK 层，返回原始结构，业务侧再映射自己的 Pydantic 模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


class Cloud115CookieStatus(str, Enum):
    """115 登录态探测结果；临时上游故障不能等同于凭据失效。"""

    ALIVE = "alive"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


class RapidUploadStatus(str, Enum):
    """本地文件执行 115 秒传初始化后的业务结果。"""

    SUCCESS = "success"
    NOT_HIT = "not_hit"
    FILE_CHANGED = "file_changed"


@dataclass(frozen=True, slots=True)
class RapidUploadResult:
    """秒传结果；未命中时不会触发普通上传。"""

    status: RapidUploadStatus
    path: str
    filename: str
    size: int
    sha1: str
    file_id: str | None = None
    pickcode: str | None = None
    raw_response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DirEntry:
    """115 目录列表返回的单条目。文件与目录共用此结构，用 is_dir 区分。

    字段来自 webapi.115.com/files 响应的短名字段：
        fid -> entry_id (文件) / cid -> entry_id (目录)
        pid -> parent_id (目录)  / cid -> parent_id (文件)
        n -> name, s -> size, sha -> sha1, pc -> pickcode
        te -> mtime, tp -> ctime, iv -> is_video
    """

    entry_id: str
    parent_id: str
    name: str
    is_dir: bool
    size: int
    sha1: str | None
    pickcode: str
    mtime: int
    ctime: int
    is_video: bool
    # play_long：已转码视频的时长秒（/files 列表响应白给，导入落库不用额外请求）；
    # 未转码新文件 / 非视频 / 目录条目为 None，上层不要把 None 当 0 落库。
    play_long: int | None = None
    # ic（is_collect）：115 违规封禁标记。1 = 已封禁（拿不到直链也播不了），
    # 导入侧应直接标 invalid + 报告；响应未带该字段时为 None。
    ic: int | None = None


@dataclass(frozen=True, slots=True)
class FileMeta:
    """/files/get_info 返回的单文件明细。"""

    file_id: str
    parent_id: str
    name: str
    size: int
    sha1: str
    pickcode: str
    mtime: int
    ctime: int
    is_video: bool


@dataclass(frozen=True, slots=True)
class DirBreadcrumb:
    """面包屑一节：目录路径链上一环。

    115 服务端在响应里给 [{"file_id": 0, "file_name": "根目录"}, ...]，本类只提取
    这两个字段作为强类型面包屑。
    """

    file_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DirMeta:
    """/category/get 返回的目录元信息。

    与 DirEntry 互补：DirEntry 是父目录 list_dir 时看到的"某个子目录"；DirMeta 是
    直接查这个目录自身，能拿到面包屑 paths、内容统计、总时长（若含视频）等。

    根目录 cid="0" 走 115 服务端会 errNo=1001，SDK 层直接构造哨兵值返回
    （name="根目录"、pickcode=""、paths=[]），调用方不必特判 cid == "0"。
    """

    cid: str
    name: str
    pickcode: str                              # 目录 pickcode；根目录哨兵为空串
    parent_id: str                             # 父目录 cid；从 paths 末尾解析；根目录哨兵为空串
    file_count: int                            # 目录直接内容总数（含子目录）；根目录哨兵为 0
    folder_count: int                          # 直接子目录数；根目录哨兵为 0
    play_long_seconds: int                     # 目录内视频总时长秒（115 服务端聚合）；无视频则 0
    mtime: int                                 # 目录最后修改 unix 秒；根目录哨兵为 0
    ctime: int                                 # 目录创建 unix 秒；根目录哨兵为 0
    paths: tuple["DirBreadcrumb", ...]         # 面包屑链：从根目录到父级（不含当前目录）；根目录哨兵为 ()


@dataclass(frozen=True, slots=True)
class DirectUrl:
    """downurl 返回的可播放/下载 302 直链。

    user_agent 必须回传给调用方：115 把 UA 绑定进 URL 的 f= 指纹，
    后续 Range GET 若换 UA 会 403。调用方（比如 /stream 端点）必须一字不差复用。

    expires_at 从 URL 的 t= 查询参数解出，unix 秒；-1 表示 URL 里未含该字段。
    （注：实测 t= 约十几小时后，非官方文档所称的 5 分钟；真实链寿命未逐一验证，以 t= 为准。）

    并发/读取（真机实测）：单条直链**顺序 Range 读很稳**，一条链就能服务数十 MB
    （整片每 10s 抽一帧、190 帧、下载 49MB、198 个 Range 全 206，无需换链，未见字节配额或限流）。
    但**每条链约 4 并发上限**：对同一 URL 同时发起 ≥5 个 Range 会零星 403。所以抽帧/播放要走
    **受控顺序（或 ≤4 并发）Range 读**；不要把 ffmpeg / PyAV 默认 http 解复用器直接指向本 URL——
    它 open/seek 时的突发请求会撞上并发上限，并被 ffmpeg 当作数据损坏报 InvalidData（SDK 调试时踩过）。
    需要抽多帧时，给解码器喂一个自己控制的顺序 HTTP 数据源，而非裸 URL。
    """

    file_id: str
    file_name: str
    file_size: int
    sha1: str
    pickcode: str
    url: str
    user_agent: str
    expires_at: int


# ============================================================
# 离线下载相关数据类型
# ============================================================


@dataclass(frozen=True, slots=True)
class OfflineTask:
    """离线下载任务一条。

    来源：GET https://115.com/web/lixian/?ct=lixian&ac=task_lists  响应里的 tasks[i]。
    完成后（status=2），file_id / pickcode 才有值，可直接接 pickcode_info / get_download_url。
    """

    info_hash: str                     # 任务唯一 ID（40 字符 hex；BT 就是种子的 info_hash）
    name: str                          # 任务名（BT 是种子内顶层目录名或单文件名；URL 是文件名）
    size: int                          # 总字节数
    status: int                        # -1=失败, 0=待办, 1=进行中, 2=完成
    status_text: str                   # 服务端翻译好的中文文案："下载中" / "下载成功" / "下载失败"
    percent_done: float                # 0.0-100.0
    rate_download: int                 # 当前下载速率 字节/秒；完成或未启动为 0
    peers: int                         # 种子 peer 数；非 BT 为 0
    left_time_seconds: int             # 服务端估计剩余秒；未启动或已完成为 0
    add_time: int                      # 任务添加 unix 秒
    last_update: int                   # 最后进度更新 unix 秒
    file_id: str                       # 完成后云盘文件 id；未完成为空串
    pickcode: str                      # 完成后云盘 pickcode；未完成为空串
    save_dir_id: str                   # 保存到的目录 cid（wp_path_id）；有时为空
    source_url: str                    # 原提交 URL；BT 情形通常为空
    retry_count: int                   # 已重试次数
    retry_limit: int                   # 服务端限制的最大重试


@dataclass(frozen=True, slots=True)
class OfflineTaskPage:
    """list_offline_tasks 分页返回。"""

    page: int                          # 当前页（1-based）
    page_count: int                    # 总页数
    page_size: int                     # 每页大小
    total_tasks: int                   # 任务总数（跨页）
    tasks: list["OfflineTask"]


@dataclass(frozen=True, slots=True)
class OfflineQuota:
    """离线下载月度配额。

    115 对每个账号有每月离线下载次数限制（非 VIP ~5 次/月，VIP ~200 次/月）。
    每提交 1 条 add_offline_urls 扣 1 次配额；BT 添加同样按 info_hash 扣 1 次。
    """

    total: int                         # 每月配额总数
    remaining: int                     # 本月剩余次数


@dataclass(frozen=True, slots=True)
class OfflineTaskAddResult:
    """add_offline_urls 单条 URL 的提交结果。

    115 支持批量提交，逐条返回 info_hash。同一 URL 若已在离线任务列表里，
    115 会返回旧任务的 info_hash（不重复计费），但也可能返回 errno 表示重复。
    """

    info_hash: str                     # 服务端分配的任务 ID；提交失败时为空串
    url: str                           # 原提交 URL（回传，便于上层 map URL -> info_hash）


# ============================================================
# 扫码登录相关数据类型（获取 cookies，见 qrlogin.Cloud115QrLogin）
# ============================================================


class QrStatus(IntEnum):
    """扫码状态。get_qrcode_status 长轮询返回。

    WAITING 也覆盖"长轮询超时无变化"（115 无变化时阻塞 ~30s 后返回空 data）；
    扫码 / 确认 / 过期 / 取消会立即打断长轮询返回对应态。
    """

    WAITING = 0     # 待扫描
    SCANNED = 1     # 已扫描，等手机确认
    CONFIRMED = 2   # 已确认登录 -> 可 fetch_result 换 cookie
    EXPIRED = -1    # 二维码过期
    CANCELED = -2   # 用户取消


@dataclass(frozen=True, slots=True)
class QrCodeToken:
    """一次扫码会话的令牌（get_token 返回）。

    uid/time/sign 用于后续轮询状态与换 cookie；qrcode 是二维码内容串
    （可自行渲染成图，或用 get_qrcode_image(uid) 直接拿 115 现成 PNG）。
    """

    uid: str
    time: int
    sign: str
    qrcode: str


@dataclass(frozen=True, slots=True)
class QrLoginResult:
    """扫码登录成功后的结果（fetch_result 返回）。

    cookies 是拼好的完整 cookie 串，可直接喂 `Cloud115Client(cookies=...)`。
    app 是本次绑定的登录槽（web/android/.../alipaymini 等）。
    """

    cookies: str
    cookie_dict: dict[str, str]
    user_id: str
    app: str
