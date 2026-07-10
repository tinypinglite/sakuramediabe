"""115 SDK 对外数据类型。

三个 frozen dataclass：DirEntry（目录条目）/ FileMeta（单文件详情）/ DirectUrl（302 直链）。
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
