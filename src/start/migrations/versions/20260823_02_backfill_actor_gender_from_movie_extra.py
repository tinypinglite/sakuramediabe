"""从影片详情 JSON 回填演员性别。"""

from __future__ import annotations

name = "20260823_02_backfill_actor_gender_from_movie_extra"


def migrate(database) -> None:
    """只使用影片详情中的明确性别，修复历史 Actor.gender。"""
    database.execute_sql(
        """
        WITH actor_gender_candidates AS (
            SELECT
                m.id AS movie_id,
                actor_item.value ->> 'id' AS javdb_id,
                actor_item.value ->> 'gender' AS raw_gender
            FROM movie AS m
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(
                        jsonb_extract_path(
                            NULLIF(BTRIM(m.extra), '')::jsonb,
                            'data', 'movie', 'actors'
                        )
                    ) = 'array'
                    THEN jsonb_extract_path(
                        NULLIF(BTRIM(m.extra), '')::jsonb,
                        'data', 'movie', 'actors'
                    )
                    ELSE '[]'::jsonb
                END
            ) AS actor_item(value)
            WHERE m.extra IS NOT NULL
              AND actor_item.value ->> 'id' IS NOT NULL
              AND actor_item.value ->> 'gender' IN ('0', '1')
        ),
        actor_gender AS (
            SELECT DISTINCT ON (javdb_id)
                javdb_id,
                CASE raw_gender
                    WHEN '0' THEN 1
                    WHEN '1' THEN 2
                END AS gender
            FROM actor_gender_candidates
            ORDER BY javdb_id, movie_id DESC, raw_gender DESC
        )
        UPDATE actor AS a
        SET gender = actor_gender.gender
        FROM actor_gender
        WHERE a.javdb_id = actor_gender.javdb_id
          AND a.gender IS DISTINCT FROM actor_gender.gender
        """
    )
