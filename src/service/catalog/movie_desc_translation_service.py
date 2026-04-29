from __future__ import annotations

import re
import time
from pathlib import Path

from loguru import logger
from peewee import Case, fn

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import Actor, Movie, MovieActor, ResourceTaskState
from src.service.catalog.movie_desc_translation_client import (
    MovieDescTranslationClient,
    MovieDescTranslationClientError,
)
from src.service.system.resource_task_state_service import ResourceTaskStateService


class MovieDescTranslationTaskAbortError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class MovieDescTranslationService:
    TASK_KEY = "movie_desc_translation"
    TRANSLATION_STATUS_PENDING = ResourceTaskStateService.STATE_PENDING
    TRANSLATION_STATUS_RUNNING = ResourceTaskStateService.STATE_RUNNING
    TRANSLATION_STATUS_SUCCEEDED = ResourceTaskStateService.STATE_SUCCEEDED
    TRANSLATION_STATUS_FAILED = ResourceTaskStateService.STATE_FAILED
    INTERRUPTED_TRANSLATION_ERROR_MESSAGE = "影片简介翻译任务中断，等待重试"
    TRANSLATION_MAX_RETRIES = 3
    TRANSLATION_RETRY_DELAY_SECONDS = 5
    DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "movie_desc_translation.md"

    def __init__(
        self,
        *,
        translation_client: MovieDescTranslationClient | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self.translation_client = translation_client or MovieDescTranslationClient()
        self.prompt_path = Path(prompt_path) if prompt_path is not None else self.DEFAULT_PROMPT_PATH

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @classmethod
    def recover_interrupted_running_movies(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_TRANSLATION_ERROR_MESSAGE
        # 仅把遗留在 running 的翻译任务重置为 failed，保留历史尝试信息。
        return ResourceTaskStateService.recover_running_records(cls.TASK_KEY, normalized_error)

    @staticmethod
    def _subscribed_actor_exists_expression():
        # 用 EXISTS 判断影片是否关联已订阅女优，避免 join 放大结果集。
        subscribed_actor_movies = (
            MovieActor.select(MovieActor.id)
            .join(Actor, on=(MovieActor.actor == Actor.id))
            .where(
                MovieActor.movie == Movie.id,
                Actor.is_subscribed == True,
            )
        )
        return fn.EXISTS(subscribed_actor_movies)

    @classmethod
    def _candidate_priority_expression(cls):
        subscribed_actor_exists = cls._subscribed_actor_exists_expression()
        # 翻译优先级固定为：已订阅影片 -> 订阅女优影片 -> 其他影片。
        return Case(
            None,
            (
                (Movie.is_subscribed == True, 0),
                (subscribed_actor_exists, 1),
            ),
            2,
        )

    @classmethod
    def _candidate_query(cls):
        matching_state_query = ResourceTaskState.select(ResourceTaskState.id).where(
            ResourceTaskState.task_key == cls.TASK_KEY,
            ResourceTaskState.resource_type == "movie",
            ResourceTaskState.resource_id == Movie.id,
        )
        priority_order = cls._candidate_priority_expression()
        subscribed_time_is_null_order = Case(
            None,
            # 已订阅影片里，把缺失订阅时间的记录排到后面；所有分支统一返回整数排序位。
            ((((Movie.is_subscribed == True) & Movie.subscribed_at.is_null()), 1),),
            0,
        )
        subscribed_time_order = Case(
            None,
            ((Movie.is_subscribed == True, Movie.subscribed_at),),
            None,
        )
        non_subscribed_heat_order = Case(
            None,
            ((Movie.is_subscribed == True, 0),),
            Movie.heat,
        )
        return (
            Movie.select(Movie)
            .where(
                Movie.desc != "",
                Movie.desc_zh == "",
                (
                    ~fn.EXISTS(matching_state_query)
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state.in_(
                                [
                                    cls.TRANSLATION_STATUS_PENDING,
                                    cls.TRANSLATION_STATUS_FAILED,
                                ]
                            )
                        )
                    )
                ),
            )
            .order_by(
                priority_order.asc(),
                subscribed_time_is_null_order.asc(),
                subscribed_time_order.asc(),
                non_subscribed_heat_order.desc(),
                Movie.id.desc(),
            )
        )

    def _load_prompt(self) -> str:
        if not self.prompt_path.exists():
            raise FileNotFoundError(f"movie_desc_translation_prompt_missing: {self.prompt_path}")
        prompt_text = self.prompt_path.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise ValueError(f"movie_desc_translation_prompt_empty: {self.prompt_path}")
        return prompt_text

    @staticmethod
    def _normalize_translated_text(translated_text: str) -> str:
        normalized_text = re.sub(r"<think>.*?</think>", "", translated_text, flags=re.DOTALL).strip()
        # 这里处理的是当前 prompt 约定的业务语义，不是通用的 LLM 响应解析规则。
        if "无有效内容" in normalized_text:
            return ""
        return normalized_text

    @classmethod
    def _mark_translation_started(cls, movie: Movie) -> None:
        # 翻译状态统一落到资源任务表，避免继续膨胀 movie 主表。
        ResourceTaskStateService.mark_started(cls.TASK_KEY, movie.id)

    @classmethod
    def _mark_translation_succeeded(cls, movie: Movie, translated_text: str) -> None:
        movie.desc_zh = translated_text
        movie.save(only=[Movie.desc_zh])
        ResourceTaskStateService.mark_succeeded(cls.TASK_KEY, movie.id)

    @classmethod
    def _mark_translation_failed(cls, movie: Movie, detail: str) -> None:
        ResourceTaskStateService.mark_failed(cls.TASK_KEY, movie.id, detail)

    @classmethod
    def _mark_translation_pending(cls, movie: Movie, detail: str) -> None:
        # 任务级中断时把影片恢复为 pending，避免误记为影片本身翻译失败。
        ResourceTaskStateService.mark_pending(cls.TASK_KEY, movie.id, detail=detail)

    @staticmethod
    def _normalize_error_message(exc: Exception) -> str:
        if isinstance(exc, MovieDescTranslationClientError):
            return exc.message
        return str(exc)

    @classmethod
    def _build_task_abort_message(cls, *, movie: Movie | None = None, detail: str) -> str:
        normalized_detail = (detail or "").strip() or cls.INTERRUPTED_TRANSLATION_ERROR_MESSAGE
        if movie is None:
            return normalized_detail
        return f"影片简介翻译任务中断 movie_number={movie.movie_number} detail={normalized_detail}"

    @staticmethod
    def _should_retry_then_abort_task(exc: MovieDescTranslationClientError) -> bool:
        return exc.should_retry_then_abort_task

    @classmethod
    def _retry_delay_seconds(cls, retry_index: int) -> int:
        # 翻译服务出现瞬时错误时按指数退避，避免短时间内持续打满上游。
        return cls.TRANSLATION_RETRY_DELAY_SECONDS * (2 ** retry_index)

    def _load_prompt_or_abort(self) -> str:
        try:
            return self._load_prompt()
        except Exception as exc:
            raise MovieDescTranslationTaskAbortError(
                self._build_task_abort_message(detail=str(exc))
            ) from exc

    def _emit_movie_progress(
        self,
        *,
        progress_callback,
        stats: dict[str, int],
        movie: Movie,
        action_text: str,
    ) -> None:
        self._emit_progress(
            progress_callback,
            current=stats["processed_movies"],
            total=stats["candidate_movies"],
            text=f"{action_text} {movie.movie_number}",
            summary_patch=stats,
        )

    def _handle_task_abort(
        self,
        *,
        movie: Movie,
        stats: dict[str, int],
        progress_callback,
        exc: MovieDescTranslationTaskAbortError,
    ) -> None:
        # 任务级中断要把当前影片退回 pending，让下一轮能从中断点继续处理。
        self._mark_translation_pending(movie, exc.message)
        logger.warning(
            "Movie desc translation aborted movie_number={} detail={}",
            movie.movie_number,
            exc.message,
        )
        self._emit_movie_progress(
            progress_callback=progress_callback,
            stats=stats,
            movie=movie,
            action_text="影片简介翻译中断",
        )

    def _handle_translation_failure(
        self,
        *,
        movie: Movie,
        stats: dict[str, int],
        progress_callback,
        exc: Exception,
    ) -> None:
        stats["failed_movies"] += 1
        error_message = self._normalize_error_message(exc)
        self._mark_translation_failed(movie, error_message)
        logger.warning(
            "Movie desc translation failed movie_number={} detail={}",
            movie.movie_number,
            error_message,
        )
        self._emit_movie_progress(
            progress_callback=progress_callback,
            stats=stats,
            movie=movie,
            action_text="影片简介翻译失败",
        )

    def _translate_with_retry(self, *, movie: Movie, system_prompt: str, source_text: str) -> str:
        last_exc: MovieDescTranslationClientError | None = None
        total_attempts = self.TRANSLATION_MAX_RETRIES + 1
        for attempt_index in range(total_attempts):
            try:
                return self.translation_client.translate(
                    system_prompt=system_prompt,
                    source_text=source_text,
                )
            except MovieDescTranslationClientError as exc:
                if not self._should_retry_then_abort_task(exc):
                    raise
                last_exc = exc
                if attempt_index + 1 >= total_attempts:
                    break
                retry_index = attempt_index
                retry_delay_seconds = self._retry_delay_seconds(retry_index)
                # 上游限流或服务异常时按指数退避重试，避免瞬时抖动直接终止整批任务。
                logger.warning(
                    "Movie desc translation retry movie_number={} retry_attempt={} retry_delay_seconds={} status_code={} error_code={} detail={}",
                    movie.movie_number,
                    retry_index + 1,
                    retry_delay_seconds,
                    exc.status_code,
                    exc.error_code,
                    exc.message,
                )
                time.sleep(retry_delay_seconds)
        detail = self._normalize_error_message(last_exc or RuntimeError(self.INTERRUPTED_TRANSLATION_ERROR_MESSAGE))
        raise MovieDescTranslationTaskAbortError(
            self._build_task_abort_message(movie=movie, detail=detail),
            status_code=last_exc.status_code if last_exc is not None else None,
            error_code=last_exc.error_code if last_exc is not None else None,
        )

    def translate_movie(self, movie: Movie) -> dict[str, int | str]:
        latest_movie = Movie.get_by_id(movie.id)
        if not str(latest_movie.desc or "").strip():
            raise ApiError(
                422,
                "movie_desc_missing",
                "影片缺少原始简介",
                {"movie_number": latest_movie.movie_number, "movie_id": latest_movie.id},
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
            # 单影片接口里不保留批处理专用的 pending 语义，直接记为本次失败。
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

    def run(
        self,
        *,
        batch_size: int | None = None,
        progress_callback=None,
    ) -> dict[str, int]:
        if not settings.movie_info_translation.enabled:
            disabled_stats = {
                "candidate_movies": 0,
                "processed_movies": 0,
                "succeeded_movies": 0,
                "failed_movies": 0,
                "updated_movies": 0,
                "skipped_movies": 0,
            }
            self._emit_progress(
                progress_callback,
                current=0,
                total=0,
                text="影片简介翻译未启用，跳过执行",
                summary_patch=disabled_stats,
            )
            return disabled_stats

        query = self._candidate_query()
        if batch_size is not None and int(batch_size) > 0:
            query = query.limit(int(batch_size))
        candidates = list(query)
        stats = {
            "candidate_movies": len(candidates),
            "processed_movies": 0,
            "succeeded_movies": 0,
            "failed_movies": 0,
            "updated_movies": 0,
            "skipped_movies": 0,
        }
        self._emit_progress(
            progress_callback,
            current=0,
            total=stats["candidate_movies"],
            text="开始翻译影片简介",
            summary_patch=stats,
        )

        if not candidates:
            return stats

        system_prompt = self._load_prompt_or_abort()

        for movie in candidates:
            stats["processed_movies"] += 1
            latest_movie = Movie.get_by_id(movie.id)
            if latest_movie.desc_zh or not latest_movie.desc:
                stats["skipped_movies"] += 1
                self._emit_movie_progress(
                    progress_callback=progress_callback,
                    stats=stats,
                    movie=latest_movie,
                    action_text="跳过无需翻译影片",
                )
                continue

            try:
                self._mark_translation_started(latest_movie)
                translated_text = self._translate_with_retry(
                    movie=latest_movie,
                    system_prompt=system_prompt,
                    source_text=latest_movie.desc,
                )
                normalized_translated_text = self._normalize_translated_text(translated_text)
                self._mark_translation_succeeded(latest_movie, normalized_translated_text)
                stats["succeeded_movies"] += 1
                stats["updated_movies"] += 1
                self._emit_movie_progress(
                    progress_callback=progress_callback,
                    stats=stats,
                    movie=latest_movie,
                    action_text="影片简介翻译成功",
                )
            except MovieDescTranslationTaskAbortError as exc:
                self._handle_task_abort(
                    movie=latest_movie,
                    stats=stats,
                    progress_callback=progress_callback,
                    exc=exc,
                )
                raise
            except Exception as exc:
                self._handle_translation_failure(
                    movie=latest_movie,
                    stats=stats,
                    progress_callback=progress_callback,
                    exc=exc,
                )

        return stats
