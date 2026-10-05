"""番号识别与规范化。

``parse_movie_number_from_text`` 的输出是查找键/检索词，不是规范值；规范形态由
元数据提供方（JavDB）返回。识别规则吸收社区实现（JavSP / JavBoss / metatube /
Emby.Plugins.JavScraper / MDC）的通用做法：

- 按 token 边界匹配，右侧不允许再跟数字，避免把长番号截成后缀
  （``NGOD-253`` 不能解析成 ``GOD-253``，``ABC-123456`` 不能截成 ``ABC-12345``）；
- 先剥离域名等噪音，剥离后解析不出再回退原文（域名本身可能就是片名载体）；
- 特例系列（FC2 / HEYZO / Tokyo-Hot / heydouga 等）优先于通用规则；
- 容易误伤的短系列（N/K）放在通用规则之后；
- 通用规则保留片商补零形态（026 保持 026），只折叠成对的前导 00
  （SSIS00123 -> SSIS-123，ADN00277 -> ADN-277）。

匹配顺序即优先级，先命中先返回。修改规则时同步维护
``tests/common/test_movie_number_corpus.py`` 里的回归语料。
"""

import re
from collections.abc import Callable
from re import Match, Pattern

Formatter = Callable[[Match[str]], str]


def _joined(joiner: str) -> Formatter:
    def build(match: Match[str]) -> str:
        return joiner.join(str(part).upper() for part in match.groups())

    return build


def _trim_pair_zeros(digits: str) -> str:
    # 片商补零形态：00277 -> 277、00068 -> 068；不足 3 位的补零段（0024）原样保留。
    if digits.startswith("00") and len(digits) - 2 >= 3:
        return digits[2:]
    return digits


def _letter_number(match: Match[str]) -> str:
    # z/Z 后缀参与番号本体（JavDB 同时存在 IBW-478 与 IBW-478z 两条记录）。
    suffix = (match.group(3) or "").upper()
    return f"{match.group(1).upper()}-{_trim_pair_zeros(match.group(2))}{suffix}"


# (正则, 取值函数)。按顺序取第一条命中的规则，返回值即大写的番号查找键。
MOVIE_NUMBER_PATTERNS: list[tuple[Pattern[str], Formatter]] = [
    # FC2：兼容 FC2PPV-123 / FC2-PPV-123 / FC2 PPV 123 / (FC2)(123)，统一折叠为 FC2-123。
    (
        re.compile(
            r"(?<![a-zA-Z0-9])FC2[^a-zA-Z0-9]*(?:PPV[^a-zA-Z0-9]*)?(\d{1,9})(?!\d)",
            re.IGNORECASE,
        ),
        lambda match: f"FC2-{match.group(1)}",
    ),
    # HEYZO：补零到 4 位（HEYZO-0024 是 JavDB 规范形态）。
    (
        re.compile(
            r"(?<![a-zA-Z0-9])HEYZO[^a-zA-Z0-9]*(?:(?:HD|LT)[^a-zA-Z0-9]*)?(\d{1,4})(?!\d)",
            re.IGNORECASE,
        ),
        lambda match: f"HEYZO-{match.group(1).zfill(4)}",
    ),
    # heydouga 三段式：heydouga-4030-2113 / hey-4030-2113。第三段 3-5 位，
    # 超长时去掉一个前导 0（04144 -> 4144），三位补零段（080）原样保留。
    (
        re.compile(
            r"(?<![a-zA-Z0-9])(?:HEYDOUGA|HEY)[^a-zA-Z0-9]*(\d{4})[^a-zA-Z0-9]*0?(\d{3,5})(?!\d)",
            re.IGNORECASE,
        ),
        lambda match: f"HEYDOUGA-{match.group(1)}-{match.group(2)}",
    ),
    # Tokyo-Hot 的 red / sky / ex 系列：番号本身不含横杠。
    (
        re.compile(r"(?<![a-zA-Z0-9])(RED\d{3}|SKY\d{3}|EX\d{4})(?!\d)", re.IGNORECASE),
        _joined(""),
    ),
    (re.compile(r"(DSVR)0(\d{3,4})", re.IGNORECASE), _joined("-")),
    (re.compile(r"(XXX)-(AV)-(\d+)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(LAFB?D?)-(\d+)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(MISM)-(\d+)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(MKB?D?)-(S\d{2,3})(?!\d)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(S2MB?D?)[-_]?(\d{2,3})(?!\d)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(CWPB?D?)-(\d+)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(SMB?D?)-(\d+)", re.IGNORECASE), _joined("-")),
    (re.compile(r"(MCDV)-(\d+)", re.IGNORECASE), _joined("-")),
    # TMA 的 T28/T38 系列（番号形态很乱，需要单独识别）。
    (re.compile(r"(?<![a-zA-Z0-9])(T[23]8)[-_]?(\d{3})(?!\d)", re.IGNORECASE), _joined("-")),
    (
        re.compile(r"(?<![a-zA-Z0-9])(MK3D2DBD|MCB3DBD)[-_]?(\d{1,3})(?!\d)", re.IGNORECASE),
        _joined("-"),
    ),
    # IBW 的 z 后缀参与番号本体（JavDB 存 IBW-411z）。
    (
        re.compile(r"(?<![a-zA-Z0-9])(IBW)[-_]?(\d{2,5})([zZ]?)(?!\d)", re.IGNORECASE),
        lambda match: f"IBW-{match.group(2)}{match.group(3).upper()}",
    ),
    # 素人系数字番号：分隔符本身是片商标识（一本道 ``_`` / 加勒比 ``-``，同日番号是两部不同影片），
    # 捕获进组原样保留、拼接符为空串，绝不转写。
    (re.compile(r"(?<!\d)(\d{6})([-_])(\d{2,3})(?!\d)"), _joined("")),
    (re.compile(r"(?<![a-zA-Z0-9])9([a-zA-Z]{3,5})(\d{2,3})(?!\d)"), _joined("-")),
    # 通用字母番号：右侧不允许再跟数字，避免截断更长的数字段；左侧不允许紧跟字母，
    # 允许数字（站点日期前缀直接粘番号的发布习惯，如 0602meyd424）。
    (re.compile(r"(?<![a-zA-Z])([a-zA-Z]{2,10})[-_](\d{2,5})([zZ]?)(?!\d)"), _letter_number),
    (re.compile(r"(?<![a-zA-Z])([a-zA-Z]{2,10})(\d{2,5})([zZ]?)(?!\d)"), _letter_number),
    (re.compile(r"(?<![a-zA-Z])([a-zA-Z]{3,5}) (\d{2,6})", re.IGNORECASE), _joined("-")),
    # Tokyo-Hot 的 n/k 系列（单字母前缀，放在通用规则之后避免误伤 ADN 这类字母串）。
    (re.compile(r"(?<![a-zA-Z0-9])([NK]\d{4})(?!\d)", re.IGNORECASE), _joined("-")),
]

# 域名/站点水印：主机名只允许字母数字（不含 ``-``，避免把 ``n1335-kan224.com`` 的
# 番号前缀一起吃掉）；TLD 后允许紧跟 ``_``、``]`` 等非字母数字字符
# （hjd2048.com_ABP821 必须整段剥掉而不是让 hjd2048 冒充番号）。
_DOMAIN_PATTERN = re.compile(
    r"(?:[a-zA-Z0-9]+\.)+"
    r"(?:com|cn|net|org|gov|edu|me|la|cc|tv|info|xyz|vip|one|app|biz|top|pw|io|co|jp|site|online)"
    r"(?![a-zA-Z0-9])",
    re.IGNORECASE,
)


# 发片组水印（hhb / hhb1 / hhb2 是常见的压制组标记，会粘在番号数字后面）。
_SOURCE_TAG_PATTERN = re.compile(r"(?i)hhb\d*")


def remove_disturb(value: str) -> str:
    return _SOURCE_TAG_PATTERN.sub("", _DOMAIN_PATTERN.sub("", value))


def _match_rules(text: str) -> str:
    for pattern, formatter in MOVIE_NUMBER_PATTERNS:
        result = pattern.search(text)
        if result:
            return formatter(result)
    return ""


def parse_movie_number_from_text(value: str) -> str:
    """从自由文本（文件名、种子名、用户输入）里**识别**番号，解析不出返回空串。

    输出是查找键/检索词，不是规范值：正则可能截断超长前缀、吃掉边缘字符，规范形态只来自
    provider（JavDB）。扫描范围由调用方决定——传整条路径还是只传文件名，是调用方的领域知识
    （导入 provider 返回的文件名与相对 ref 使用同一截断策略）。
    """
    text = value or ""
    cleaned = remove_disturb(text)
    parsed = _match_rules(cleaned)
    if not parsed and cleaned != text:
        # 域名剥离可能连带吃掉番号本体（如 UUS75.COM），剥完无果时回退原文。
        parsed = _match_rules(text)
    return parsed


def normalize_movie_number(value: str) -> str:
    """番号匹配键；纯数字番号保留分隔符，其他番号沿用宽松匹配。

    仅用于匹配，不用于落库改写。人工输入查库走 movie_number_lookup_values。
    """
    normalized = (value or "").strip().upper()
    normalized = normalized.replace(" ", "")
    if re.fullmatch(r"\d+[-_]\d+", normalized):
        return normalized
    normalized = normalized.replace("_", "-")
    normalized = normalized.replace("PPV-", "")
    return normalized


def movie_number_lookup_values(value: str) -> list[str]:
    """人工输入的等值候选集；纯数字番号不互换分隔符，其他番号先原形后互换。"""
    stripped = (value or "").strip().upper()
    if not stripped:
        return []
    if re.fullmatch(r"\d+[-_]\d+", stripped):
        return [stripped]
    candidates = [stripped]
    for swapped in (stripped.replace("_", "-"), stripped.replace("-", "_")):
        if swapped not in candidates:
            candidates.append(swapped)
    return candidates


def subtitle_matches_movie_number(subtitle_name: str, movie_number: str) -> bool:
    """字幕文件名解析出的番号与影片番号一致才算配对（纯番号匹配，无同名兜底）。

    不同来源导入共用同一判定：从字幕文件名解析番号，解析不出直接判否；
    解析出的番号与影片番号经 normalize_movie_number 归一后相等才视为同一部影片的字幕。
    """
    parsed = parse_movie_number_from_text(subtitle_name)
    if not parsed:
        return False
    return normalize_movie_number(parsed) == normalize_movie_number(movie_number)
