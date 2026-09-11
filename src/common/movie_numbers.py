import re
from re import Pattern

# (正则, 捕获组拼接符)。按顺序取第一条命中的规则，输出 = 拼接符 join 各捕获组后大写。
MOVIE_NUMBER_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"(DSVR)0(\d{3,4})", re.IGNORECASE), "-"),
    (re.compile(r"(XXX)-(AV)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(N\d{4})", re.IGNORECASE), "-"),
    (re.compile(r"(LAFB?D?)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(MISM)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(MKB?D?)-(S\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(S2MB?D?)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(CWPB?D?)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(SMB?D?)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(MCDV)-(\d+)", re.IGNORECASE), "-"),
    # 素人系数字番号：分隔符本身是片商标识（一本道 ``_`` / 加勒比 ``-``，同日番号是两部不同影片），
    # 捕获进组原样保留、拼接符为空串，绝不转写。
    (re.compile(r"(\d{6})([-_])(\d{3})"), ""),
    (re.compile(r"(FC2)PPV_(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(FC2)PPV-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(FC2)-PPV-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"(FC2)-(\d+)", re.IGNORECASE), "-"),
    (re.compile(r"9([a-zA-Z]{3,5})(\d{2,3})"), "-"),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})00(\d{3})"), "-"),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})-(\d{3,5})"), "-"),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})(\d{3,5})"), "-"),
    (re.compile(r"([a-zA-Z]{3,5}) (\d{2,6})"), "-"),
]


def remove_disturb(value: str) -> str:
    pattern = r"\b(?:[a-zA-Z0-9]+\.)*[a-zA-Z0-9]+\.(?:com|cn|net|org|gov|edu)\b"
    return re.sub(pattern, "", value)


def parse_movie_number_from_text(value: str) -> str:
    """从自由文本（文件名、种子名、用户输入）里**识别**番号，解析不出返回空串。

    输出是查找键/检索词，不是规范值：正则可能截断超长前缀、吃掉边缘字符，规范形态只来自
    provider（JavDB）。扫描范围由调用方决定——传整条路径还是只传文件名，是调用方的领域知识
    （导入 provider 返回的文件名与相对 ref 使用同一截断策略）。
    """
    text = remove_disturb(value or "")
    for pattern, joiner in MOVIE_NUMBER_PATTERNS:
        result = pattern.search(text)
        if result:
            return joiner.join(str(part).upper() for part in result.groups())
    return ""


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
