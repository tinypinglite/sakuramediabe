from __future__ import annotations

from peewee import IntegrityError

from src.i18n import translate
from src.i18n.locale import current_language, normalize_language
from src.model import MovieTag, Tag, get_database
from src.service.catalog.tag_name_map import resolve_tag_name


class TagNameNormalizeService:
    TASK_KEY = "tag_name_normalize"

    @classmethod
    def normalize_tags(cls, reporter, language: str | None = None) -> dict[str, int]:
        target_language = normalize_language(language) or current_language()
        target_language = normalize_language(target_language) or "zh-CN"
        rows = list(Tag.select(Tag.id, Tag.name).order_by(Tag.id).tuples())
        stats = {
            "scanned": len(rows),
            "unchanged": 0,
            "renamed": 0,
            "merged": 0,
            "links_moved": 0,
        }

        for index, (tag_id, name) in enumerate(rows, start=1):
            target_name = resolve_tag_name(name, target_language)
            if target_name == name:
                stats["unchanged"] += 1
            else:
                action = cls._move_tag(tag_id, target_name, stats)
                if action == "renamed":
                    stats["renamed"] += 1
                elif action == "merged":
                    stats["merged"] += 1
                else:
                    stats["unchanged"] += 1

            if index == len(rows) or index % 25 == 0:
                reporter.emit(
                    current=index,
                    total=len(rows),
                    text=translate(
                        "tasks.tag_name_normalize.progress.running",
                        current=index,
                        total=len(rows),
                        renamed=stats["renamed"],
                        merged=stats["merged"],
                        unchanged=stats["unchanged"],
                    ),
                    summary_patch=stats,
                )

        return stats

    @classmethod
    def _move_tag(
        cls, tag_id: int, target_name: str, stats: dict[str, int]
    ) -> str:
        with get_database().atomic():
            tag = Tag.get_or_none(Tag.id == tag_id)
            if tag is None:
                return "missing"

            target_tag = Tag.get_or_none(Tag.name == target_name)
            if target_tag is not None and target_tag.id != tag.id:
                stats["links_moved"] += cls._merge_into(tag, target_tag)
                return "merged"

            try:
                tag.name = target_name
                tag.save(only=[Tag.name])
                return "renamed"
            except IntegrityError:
                target_tag = Tag.get(Tag.name == target_name)
                stats["links_moved"] += cls._merge_into(tag, target_tag)
                return "merged"

    @staticmethod
    def _merge_into(source: Tag, target: Tag) -> int:
        cursor = get_database().execute_sql(
            """
            INSERT INTO movie_tag (movie_id, tag_id)
            SELECT movie_id, %s FROM movie_tag WHERE tag_id = %s
            ON CONFLICT (movie_id, tag_id) DO NOTHING
            """,
            (target.id, source.id),
        )
        moved = max(cursor.rowcount, 0)
        MovieTag.delete().where(MovieTag.tag == source).execute()
        source.delete_instance()
        return moved
