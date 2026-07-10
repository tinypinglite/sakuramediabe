"""115 SDK 对外数据类型。

全部为 frozen dataclass：DirEntry / FileMeta / DirMeta / DirectUrl / VideoInfo 等。
故意与 Pydantic schema 解耦：这是底层 SDK 层，返回原始结构，业务侧再映射自己的 Pydantic 模型。
"""

from __future__ import annotations

from dataclasses import dataclass


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
    """

    file_id: str
    file_name: str
    file_size: int
    sha1: str
    pickcode: str
    url: str
    user_agent: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class VideoDefinition:
    """master m3u8 里的一个清晰度分支（HLS BANDWIDTH variant）。

    115 视频经服务端转码后按不同码率产出多个 variant m3u8；ts 分段在 variant 层。
    """

    bandwidth: int             # BANDWIDTH 属性，bit/s
    resolution: str            # RESOLUTION 属性，如 "1280x720"；未声明时为空串
    label: str                 # NAME 属性，如 "HD"；未声明时为空串
    m3u8_url: str              # variant m3u8 的绝对 URL


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """webapi.115.com/files/video 返回的视频综合信息 + master m3u8 清晰度列表。

    仅对 VIP 会员可用；非会员账号 errno=406 → Cloud115MembershipRequiredError。
    """

    pickcode: str
    width: int                 # 原始视频宽（像素）；不确定时 0
    height: int                # 原始视频高（像素）；不确定时 0
    thumb_url: str             # 封面缩略图 URL；缺省时空串
    master_m3u8_url: str       # HLS master playlist 绝对 URL
    definitions: list["VideoDefinition"]   # 所有可用清晰度


@dataclass(frozen=True, slots=True)
class VideoSegment:
    """variant m3u8 里的一个 HLS ts 分段。

    每段是一小段独立可解码的视频，天然对应"每 duration_seconds 抽一帧"的场景 ——
    上层 ffmpeg 对 url 直接 `-ss 0 -vframes 1` 就能拿到本段起始帧。
    """

    index: int                 # 0-based 序号
    url: str                   # 绝对 URL（相对路径已用 variant m3u8URL 拼过）
    duration_seconds: float    # EXTINF 声明的时长，秒


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


@dataclass(frozen=True, slots=True)
class TorrentFileEntry:
    """种子内的单个文件条目（来自 ac=torrent 响应的 torrent_filelist_web[i]）。

    index 就是数组下标（0-based）——add_task_bt 的 wanted 参数用的正是这个下标。
    wanted 是 115 给的**默认勾选态**：True=默认下载，False=115 已自动反选
    （常见于广告/引流类垃圾文件，真机实测 `manko.fun.url` 42B 会被默认反选）。
    上层把这个默认态展示给用户勾选，最终选中的 index 列表回传 add_task_bt。
    """

    index: int
    path: str                          # 种子内相对路径（可能带子目录）
    size: int                          # 字节
    wanted: bool                       # 115 默认是否勾选下载


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    """ac=torrent 解析一个已上传种子后的结果。

    info_hash 是后续 add_task_bt 的唯一入参之一（40 字符 hex，与本地对种子 info 段
    算 sha1 一致，真机已核对）。name 是种子顶层目录名，建任务时常直接用作 savepath。
    """

    info_hash: str
    name: str
    file_count: int
    files: list["TorrentFileEntry"]
