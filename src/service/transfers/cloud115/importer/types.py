"""cloud115 导入管线的公共数据类型。

拆自 ``service.py`` 顶部：scanner / strategies / registrar 都要引用同一套 dataclass，
放在 ``service.py`` 里会强制这些模块反向 import service，造成循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


@dataclass
class CloudSubtitleFile:
    """源目录里与视频配对的 .srt sidecar。"""

    fid: str
    pickcode: str
    name: str


@dataclass
class CloudSourceFile:
    """枚举 + 分拣后的单个待导入云端视频。"""

    fid: str
    pickcode: str
    name: str
    sha1: str
    size: int
    play_long: int | None
    censored: bool
    rel_dir_parts: tuple[str, ...]
    # 所在父目录 cid：字幕 sidecar 配对按同目录匹配。
    parent_cid: str = ""
    # 番号识别结果：配对与分组共用，避免重复解析（videos 域复用本 dataclass 时保持空串）。
    movie_number: str = ""
    subtitle: CloudSubtitleFile | None = None

    @property
    def rel_path(self) -> str:
        """源目录内相对路径（人可读，用于失败清单与重导匹配）。"""
        return "/".join([*self.rel_dir_parts, self.name])


@dataclass
class CloudImportGroup:
    """按番号聚合后的一组待导入云端文件（不合并，逐文件登记）。"""

    movie_number: str
    files: list[CloudSourceFile] = field(default_factory=list)


class ResolvedFile(NamedTuple):
    """copy 策略中 copy + 对账后的单个待登记条目。"""

    cloud_file: CloudSourceFile
    target_fid: str
    target_pickcode: str
    current_name: str
    target_name: str
