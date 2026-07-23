from __future__ import annotations

from src.common import normalize_movie_number
from src.config.config import settings
from src.start.migrations import SkipMigration

name = "20260723_01_revert_duration_based_collections"

# 单条 UPDATE 的 id 批量上限，避免 IN 列表参数过长。
_UPDATE_BATCH_SIZE = 1000


def migrate(database, migrator) -> None:
    # 合集判定去掉"时长阈值"后，历史上"仅因时长超阈值"被自动判为合集的影片需要恢复成单体。
    # 新规则只认番号特征前缀：对未手动覆盖(is_collection_overridden=False)且当前 is_collection=True
    # 的影片用新规则重算，不再命中前缀的回落 is_collection=False；命中前缀的本就是合集保持不变，
    # 手动覆盖的影片一律不动。currently False 的影片新旧规则结果一致，无需触碰。
    if not database.table_exists("movie"):
        raise SkipMigration("movie table does not exist")

    # others_number_features 在配置加载时已按番号习惯归一，这里再规范化一次保持与 service 判定一致。
    prefixes = [
        normalized
        for feature in settings.media.others_number_features
        if (normalized := normalize_movie_number(feature))
    ]

    rows = database.execute_sql(
        "SELECT id, movie_number FROM movie "
        "WHERE is_collection = TRUE AND is_collection_overridden = FALSE"
    ).fetchall()

    revert_ids = []
    for movie_id, movie_number in rows:
        normalized_number = normalize_movie_number(movie_number)
        # 番号规范化后不再命中任一特征前缀 => 当初是靠时长判定的合集，恢复为单体。
        if not normalized_number or not any(
            normalized_number.startswith(prefix) for prefix in prefixes
        ):
            revert_ids.append(movie_id)

    for start in range(0, len(revert_ids), _UPDATE_BATCH_SIZE):
        batch_ids = revert_ids[start:start + _UPDATE_BATCH_SIZE]
        placeholders = ",".join(["%s"] * len(batch_ids))
        database.execute_sql(
            f"UPDATE movie SET is_collection = FALSE WHERE id IN ({placeholders})",
            batch_ids,
        )
