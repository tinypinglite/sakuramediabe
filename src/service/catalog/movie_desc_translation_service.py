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

        # 单影片手动翻译与批量任务共用同一套翻译链路与 kernel 记账。
        from src.service.system.activity_service import ActivityService
        from src.service.system.resource_task_runner import ResourceTaskLedger

        run_context = ActivityService.get_task_run_context()
        attempt, record, _prior_state = ResourceTaskLedger.begin_attempt(
            task_key=self.TASK_KEY,
            resource_type="movie",
            resource_id=latest_movie.id,
            trigger_type=getattr(run_context, "trigger_type", None),
            task_run_id=getattr(run_context, "task_run_id", None),
        )
        try:
            system_prompt = self._load_prompt_or_abort()
            translated_text = self._translate_with_retry(
                movie=latest_movie,
                system_prompt=system_prompt,
                source_text=latest_movie.desc,
            )
            normalized_translated_text = self._normalize_translated_text(translated_text)
        except Exception as exc:
            # 单影片接口没有"整轮"可中止:上游不可用与业务性失败一律按可重试失败记账。
            error_message = self._normalize_error_message(exc)
            ResourceTaskLedger.finish_failure(
                attempt,
                record,
                error_code=getattr(exc, "error_code", None) or "translation_failed",
                error_message=error_message,
                retryable=True,
                policy=self.TRANSLATION_RETRY_POLICY,
            )
            logger.warning(
                "Movie desc translation failed movie_number={} detail={}",
                latest_movie.movie_number,
                error_message,
            )
            raise
        self._apply_translation(latest_movie, normalized_translated_text)
        ResourceTaskLedger.finish_success(attempt, record)
        return {
            "movie_id": latest_movie.id,
            "movie_number": latest_movie.movie_number,
            "updated_movies": 1,
        }
