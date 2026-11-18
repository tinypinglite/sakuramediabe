from contextlib import contextmanager

from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.service.transfers.shared.import_notifications import (
    create_new_media_reminder,
)


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def test_new_media_reminder_follows_current_language(test_db):
    items = [{"movie_number": "ABC-001", "movie_id": 1}]

    with _language("en"):
        english = create_new_media_reminder(movie_items=items)
    with _language("zh-CN"):
        chinese = create_new_media_reminder(movie_items=items)

    assert english.title == "New movies are ready to play"
    assert english.content == "1 new movie added"
    assert chinese.title == "有新的影片可以播放了"
    assert chinese.content == "新增了 1 个影片"
