from types import SimpleNamespace

from src.model import Movie, MovieTag, Tag
from src.service.catalog.tag_name_normalize_service import TagNameNormalizeService


def _reporter():
    events: list[dict] = []
    return SimpleNamespace(emit=lambda **payload: events.append(payload)), events


def test_normalize_merges_same_target_and_keeps_unknown_tags(test_db):
    first = Movie.create(movie_number="TAG-001", title="first")
    second = Movie.create(movie_number="TAG-002", title="second")
    big_tits = Tag.create(name="Big tits")
    giant = Tag.create(name="巨乳")
    unknown = Tag.create(name="不存在的标签")
    MovieTag.create(movie=first, tag=big_tits)
    MovieTag.create(movie=second, tag=giant)
    MovieTag.create(movie=second, tag=unknown)
    reporter, events = _reporter()

    stats = TagNameNormalizeService.normalize_tags(
        reporter=reporter, language="zh-CN"
    )

    assert stats["merged"] == 1
    assert stats["renamed"] == 0
    assert stats["unchanged"] == 2
    assert set(Tag.select(Tag.name).scalars()) == {"巨乳", "不存在的标签"}
    assert MovieTag.select().where(MovieTag.tag == giant).count() == 2
    assert events[-1]["summary_patch"] == stats


def test_normalize_renames_when_target_does_not_exist_and_is_idempotent(test_db):
    movie = Movie.create(movie_number="TAG-003", title="third")
    tag = Tag.create(name="Big tits")
    MovieTag.create(movie=movie, tag=tag)
    reporter, _ = _reporter()

    first = TagNameNormalizeService.normalize_tags(
        reporter=reporter, language="zh-CN"
    )
    second = TagNameNormalizeService.normalize_tags(
        reporter=reporter, language="zh-CN"
    )

    assert first["renamed"] == 1
    assert Tag.get_by_id(tag.id).name == "巨乳"
    assert second["renamed"] == 0
    assert second["merged"] == 0
