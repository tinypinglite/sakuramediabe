"""transfers 域内**真正跨链路 / 跨顶层域**共享的少数符号。

历史上这里堆过 30+ 个 downloads 域独占的东西（qB tag、下载状态归一、任务查询排序、
client 校验等），2026-08 拆出去到 ``transfers/downloads/common.py`` 后本文件只保留
两类东西：

1. ``canonicalize_btih``：BTIH 规范化，schema / downloads / cloud115 三个子包都要用，
   且必须共用同一个实现，否则同一 hash 的不同书写在不同链路上会被判成不同种子。
2. 三个 DownloadTask 存在性 / 死态判定的 peewee 表达式：``catalog`` 域的
   ``MovieSubscriptionService`` 与 ``transfers`` 内多处都要复用同一份 SQL 条件。
"""

import base64
import binascii
import re

from peewee import fn

from src.common.media_import_status import UNFINISHED_IMPORT_STATUSES
from src.model import DownloadTask, Movie

_BTIH_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_BTIH_BASE32_PATTERN = re.compile(r"^[A-Z2-7a-z]{32}$")


def canonicalize_btih(value: str) -> str:
    """把 hex/Base32 BTIH 严格规范化为 40 位小写 hex。

    种子在本系统里的**唯一身份**。放在 transfers 公共模块而不是某个下载器模块里：它是纯字符串
    处理，与 115 / qb / 索引器都无关，而选种、离线对账、任务删除、索引器候选四条链路都要用它，
    且必须用同一个实现——不同写法的同一个 hash（大小写、Base32）必须收敛到同一个字符串，否则
    「这个种子是不是同一个」在不同链路上会给出不同答案。
    """
    normalized = (value or "").strip()
    if _BTIH_HEX_PATTERN.fullmatch(normalized):
        return normalized.lower()
    if _BTIH_BASE32_PATTERN.fullmatch(normalized):
        try:
            decoded = base64.b32decode(normalized.upper(), casefold=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("BTIH Base32 解码失败") from exc
        if len(decoded) == 20:
            return decoded.hex()
    raise ValueError("BTIH 必须是 40 位 hex 或 32 位 Base32")


# 死态集合：本地已确知不会再有进展。判死不删记录——DownloadTask 行本身就是"这部影片试过哪个种子"
# 的台账，选种黑名单直接读它，删了黑名单就没了数据来源。
#
# 判死全部发生在**对账时**（写进 download_state），查询侧只做一次集合判定，因此这里不需要任何时间
# 参数。这一点是刻意的：判定依赖 qB 的 last_activity，而 qB 联系不上时我们不该替它宣布种子死亡，
# 对账不跑 = 状态冻结，正是想要的行为。
_DOWNLOAD_DEAD_STATES = frozenset({"failed", "abandoned", "stalled_dead"})

# 公开别名：清理服务需要在原子 UPDATE 里做"非死态"守卫（in_(...) 需要排序保证参数稳定）。
DOWNLOAD_DEAD_STATES = _DOWNLOAD_DEAD_STATES

# 候选在提交前被确认命中该影片的死种黑名单（torrent-only 候选需先解析 .torrent 才能得知身份）。
# 自动下载把它当作"不合格候选"换下一个，而不是当作提交故障消耗预算。
ERROR_CODE_CANDIDATE_DEAD = "download_candidate_dead"


def _download_task_movie_match_expression():
    """DownloadTask 与 Movie 的番号关联条件。

    **两侧都必须是裸列**：movie.movie_number 存 provider 规范原样，download_task.movie_number
    由提交链路拷贝同一列（对账重建行只填空不覆写，见 DownloadSyncService），两列直接可比。
    任何一侧套上 UPPER(TRIM()) 都会让该列索引失效，该表达式所在的相关子查询退化为逐行全表顺扫。
    """
    return DownloadTask.movie == Movie.movie_number


def download_task_dead_expression():
    """DownloadTask 是否已判死的 peewee 条件表达式，供选种黑名单与活跃任务判定复用。

    纯集合判定：判死已经在对账时完成并落进 download_state（该列有索引）。
    """
    return DownloadTask.download_state.in_(tuple(sorted(_DOWNLOAD_DEAD_STATES)))


def active_download_task_exists_expression():
    """影片是否还有"活着的"下载任务。

    判定的是活跃而非存在：failed / abandoned / stalled_dead 的任务留在库里当台账，但不再阻塞
    重新查资源——过去按"存在任何 DownloadTask"判定，死种会让那部影片永久不再被查。
    completed 但导入失败的任务仍算活跃：文件已经在盘上，该修的是导入而不是重下。
    """
    active_tasks = DownloadTask.select(DownloadTask.id).where(
        _download_task_movie_match_expression() & ~download_task_dead_expression()
    )
    return fn.EXISTS(active_tasks)


def unfinished_import_download_task_exists_expression():
    """影片是否还有"导入没跑完"的活跃下载任务——即还在下载、或下完了正等/正在导入。

    是 :func:`active_download_task_exists_expression` 的**真子集**——`~dead` 那一半必须逐字
    一致。订阅状态据此二分活跃任务：命中本表达式的是 `downloading`（还在路上），剩下的活跃
    任务是 `import_failed`（导入这一趟已经跑完，库里却没有 Media）。两个分支的并集必须恒等于
    "有活跃任务"，否则会有影片掉进"缺资源"：页面说该重新找种，而搜索闸门（同样读活跃任务）
    说别找，两边自相矛盾。

    判"在途"而不是判"失败"，是因为导入终态不止 failed：整包只有小于阈值的样本文件时，扫描
    记 skipped_count、failed_count 为 0，任务会落成 import_status=completed 却一个 Media 都
    没产出（见 MediaImportService）。按 failed 切会把这类漏在"下载中"里永远藏着。

    这类任务文件已经在盘上，该修的是导入而不是重下，因此它**不放开搜索闸门**——闸门仍读
    active_download_task_exists_expression，本表达式只服务于展示层的状态细分。
    """
    tasks = DownloadTask.select(DownloadTask.id).where(
        _download_task_movie_match_expression()
        & ~download_task_dead_expression()
        & DownloadTask.import_status.in_(UNFINISHED_IMPORT_STATUSES)
    )
    return fn.EXISTS(tasks)
