from contextlib import contextmanager

from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.service.playback.media_invalid_notification_service import (
    MediaInvalidNotificationService,
)


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def test_media_invalid_notification_follows_current_language(test_db):
    item = {
        "media_id": 12,
        "display_name": "ABC-123",
        "reason": "moov_missing",
    }

    with _language("en"):
        english = MediaInvalidNotificationService.notify_new_invalid(items=[item])
    with _language("zh-CN"):
        chinese = MediaInvalidNotificationService.notify_new_invalid(items=[item])

    assert english.title == "Invalid media detected"
    assert "ABC-123: moov is missing" in english.content
    assert chinese.title == "发现新增失效媒体"
    assert "ABC-123：缺少 moov" in chinese.content
