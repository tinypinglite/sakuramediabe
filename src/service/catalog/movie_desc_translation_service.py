from __future__ import annotations

from pathlib import Path

from src.service.catalog._movie_field_translation import (
    MovieFieldTranslationServiceBase,
    MovieFieldTranslationTaskAbortError,
)


class MovieDescTranslationTaskAbortError(MovieFieldTranslationTaskAbortError):
    """影片简介翻译任务级中断异常;保留独立类名以兼容现有 isinstance / except 用法。"""


class MovieDescTranslationService(MovieFieldTranslationServiceBase):
    TASK_KEY = "movie_desc_translation"
    SOURCE_ATTR = "desc"
    TARGET_ATTR = "desc_zh"
    FIELD_LABEL = "简介"
    LOG_LABEL = "desc"
    PROMPT_ERROR_PREFIX = "movie_desc_translation"
    INTERRUPTED_TRANSLATION_ERROR_MESSAGE = "影片简介翻译任务中断，等待重试"
    DEFAULT_PROMPT_PATH = (
        Path(__file__).resolve().parent / "prompts" / "movie_desc_translation.md"
    )
    ABORT_ERROR_CLASS = MovieDescTranslationTaskAbortError
