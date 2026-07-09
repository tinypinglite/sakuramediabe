from __future__ import annotations

from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.model import Movie
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
    SKIP_ACTION_TEXT = "跳过无需翻译影片"
    INTERRUPTED_TRANSLATION_ERROR_MESSAGE = "影片简介翻译任务中断，等待重试"
    DEFAULT_PROMPT_PATH = (
        Path(__file__).resolve().parent / "prompts" / "movie_desc_translation.md"
    )
    ABORT_ERROR_CLASS = MovieDescTranslationTaskAbortError

    def translate_movie(self, movie: Movie) -> dict[str, int | str]:
        latest_movie = Movie.get_by_id(movie.id)
        if not str(latest_movie.desc or "").strip():
            raise ApiError(
                422,
                "movie_desc_missing",
                "影片缺少原始简介",
                {
                    "movie_number": latest_movie.movie_number,
                    "movie_id": latest_movie.id,
                },
            )

        # 单影片手动翻译与批量任务共用同一套翻译链路与状态写入逻辑。
        self._mark_translation_started(latest_movie)
        try:
            system_prompt = self._load_prompt_or_abort()
            translated_text = self._translate_with_retry(
                movie=latest_movie,
                system_prompt=system_prompt,
                source_text=latest_movie.desc,
            )
            normalized_translated_text = self._normalize_translated_text(translated_text)
            self._mark_translation_succeeded(latest_movie, normalized_translated_text)
            return {
                "movie_id": latest_movie.id,
                "movie_number": latest_movie.movie_number,
                "updated_movies": 1,
            }
        except MovieDescTranslationTaskAbortError as exc:
            # 单影片接口里不保留批处理专用的 pending 语义,直接记为本次失败。
            self._mark_translation_failed(latest_movie, exc.message)
            logger.warning(
                "Movie desc translation failed movie_number={} detail={}",
                latest_movie.movie_number,
                exc.message,
            )
            raise
        except Exception as exc:
            error_message = self._normalize_error_message(exc)
            self._mark_translation_failed(latest_movie, error_message)
            logger.warning(
                "Movie desc translation failed movie_number={} detail={}",
                latest_movie.movie_number,
                error_message,
            )
            raise
