from contextlib import contextmanager

from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.service.discovery import ranking_service
from src.service.discovery.ranking_service import (
    RankingBoardDefinition,
    RankingCatalogService,
    RankingSourceDefinition,
)


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def test_ranking_names_follow_current_language(monkeypatch):
    board = RankingBoardDefinition(
        key="hot",
        name="热播",
        name_i18n={"en": "Popular"},
    )
    source = RankingSourceDefinition(
        key="demo",
        name="演示来源",
        name_i18n={"en": "Demo source"},
        boards=(board,),
    )
    monkeypatch.setattr(ranking_service, "RANKING_SOURCES", {"demo": source})

    with _language("en"):
        assert RankingCatalogService.list_sources()[0].name == "Demo source"
        assert RankingCatalogService.list_boards("demo")[0].name == "Popular"

    with _language("zh-CN"):
        assert RankingCatalogService.list_sources()[0].name == "演示来源"
        assert RankingCatalogService.list_boards("demo")[0].name == "热播"


def test_ranking_definition_positional_arguments_keep_old_order():
    board = RankingBoardDefinition(
        "hot",
        "热播",
        ("daily",),
        "daily",
        None,
        None,
        None,
    )
    source = RankingSourceDefinition("demo", "Demo", (board,), "plugin_x")

    assert board.name_i18n == {}
    assert source.name_i18n == {}
    assert source.plugin_id == "plugin_x"
