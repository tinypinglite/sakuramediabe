from __future__ import annotations

from pathlib import Path

from src.service.catalog._movie_field_translation import (
    MovieFieldTranslationServiceBase,
    MovieFieldTranslationTaskAbortError,
)


class MovieTitleTranslationTaskAbortError(MovieFieldTranslationTaskAbortError):
    """影片标题翻译任务级中断异常;保留独立类名以兼容现有 isinstance / except 用法。"""


class MovieTitleTranslationService(MovieFieldTranslationServiceBase):
    TASK_KEY = "movie_title_translation"
    SOURCE_ATTR = "title"
    TARGET_ATTR = "title_zh"
    FIELD_LABEL = "标题"
    LOG_LABEL = "title"
    PROMPT_ERROR_PREFIX = "movie_title_translation"
    SKIP_ACTION_TEXT = "跳过无需翻译影片标题"
    INTERRUPTED_TRANSLATION_ERROR_MESSAGE = "影片标题翻译任务中断，等待重试"
    DEFAULT_PROMPT_PATH = (
        Path(__file__).resolve().parent / "prompts" / "movie_title_translation.md"
    )
    ABORT_ERROR_CLASS = MovieTitleTranslationTaskAbortError
