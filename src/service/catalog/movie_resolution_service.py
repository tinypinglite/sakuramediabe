"""影片最高分辨率档位查询。"""

from peewee import Case, fn

from src.api.exception.errors import ApiError
from src.model import Media, Movie

RESOLUTION_LEVELS = (
    ("8K", 7),
    ("4K", 6),
    ("2K", 5),
    ("1080P", 4),
    ("720P", 3),
    ("480P", 2),
    ("360P", 1),
)



def resolution_level_expression():
    """4K/8K 按宽度、较低档位按高度判断，返回可聚合的档位序号。"""
    width = fn.split_part(Media.resolution, "x", 1).cast("int")
    height = fn.split_part(Media.resolution, "x", 2).cast("int")
    return Case(None, (
        ((width <= 0) | (height <= 0), 0),
        (width >= 7680, 7),
        (width >= 3840, 6),
        (height >= 1440, 5),
        (height >= 1080, 4),
        (height >= 720, 3),
        (height >= 480, 2),
        (height >= 360, 1),
    ), 0)


def resolution_interval(resolution: str | None, *, error_code: str = "invalid_playlist_filter") -> tuple[int | None, int | None]:
    """解析分辨率筛选档位为 ``[threshold, upper)`` 档位序号区间；非法档位抛 422。

    档位互斥，8K 影片不会误入 4K。
    """
    if resolution is None:
        return (None, None)
    normalized = resolution.strip().lower()
    for index, (label, threshold) in enumerate(RESOLUTION_LEVELS):
        if label.lower() == normalized:
            upper = RESOLUTION_LEVELS[index - 1][1] if index > 0 else None
            return (threshold, upper)
    raise ApiError(
        422,
        error_code,
        "Invalid resolution filter",
        {"resolution": resolution},
    )


def resolution_exists_expression(resolution: str, *, error_code: str = "invalid_playlist_filter"):
    """构造「影片最高分辨率落在档位区间」的 EXISTS 子查询。

    对 media 分组后按最高档位归类，只匹配 ``WxH`` 形态（probe 写入），
    无法解析的脏值直接排除。
    """
    threshold, upper = resolution_interval(resolution, error_code=error_code)
    level_expression = resolution_level_expression()
    having_conditions = [fn.MAX(level_expression) >= threshold]
    if upper is not None:
        having_conditions.append(fn.MAX(level_expression) < upper)
    return fn.EXISTS(
        Media.select(fn.COUNT(Media.id))
        .where(
            Media.movie == Movie.movie_number,
            Media.valid == True,
            Media.resolution.regexp(r"^\d+x\d+$"),
        )
        .group_by(Media.movie)
        .having(*having_conditions)
    )
