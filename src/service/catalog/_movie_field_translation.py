from __future__ import annotations

import re
import time
from pathlib import Path
from typing import ClassVar

from loguru import logger
from peewee import Case, fn

from src.config.config import settings
from src.model import Actor, Movie, MovieActor
from src.service.catalog.movie_desc_translation_client import (
    MovieDescTranslationClient,
    MovieDescTranslationClientError,
)
from src.service.system.resource_task_runner import (
    ResourceTaskLedger,
    ResourceTaskRunner,
    ResourceTaskSpec,
    RetryPolicy,
    TaskAbortError,
    TaskItemDeferred,
    TaskItemError,
)


class MovieFieldTranslationTaskAbortError(RuntimeError):
    # 影片字段翻译任务级中断的通用异常;具体字段(简介/标题)通过继承派生。
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


class MovieFieldTranslationServiceBase:
    """影片字段翻译服务的通用基类（已迁 kernel 记账，任务架构 Wave 2）。

    子类通过类属性声明:任务 key、源/目标字段名、prompt 路径、文案与专属 abort 异常类。
    候选筛选、逐资源记账、重试预算与进度上报由 ResourceTaskRunner 统一负责：
    - 上游不可用（限流重试耗尽 / prompt 缺失）→ TaskAbortError，当前影片回 pending
      不耗预算、整轮中止；
    - 单影片业务性失败（如内容被拒）→ TaskItemError 按预算退避，超限 exhausted
      （旧实现无预算、失败每轮无限重试，本版起收敛）。
    """

    # === 子类必须覆写的差异化配置 ===
    TASK_KEY: ClassVar[str] = ""
    SOURCE_ATTR: ClassVar[str] = ""  # movie 上的原文字段名,如 "desc" / "title"
    TARGET_ATTR: ClassVar[str] = ""  # movie 上的译文字段名,如 "desc_zh" / "title_zh"
    FIELD_LABEL: ClassVar[str] = ""  # 中文短标签,用于运行时文案,如 "简介" / "标题"
    LOG_LABEL: ClassVar[str] = ""  # 日志前缀短标签,如 "desc" / "title"
    PROMPT_ERROR_PREFIX: ClassVar[str] = ""  # prompt 缺失/为空错误 code 前缀
    INTERRUPTED_TRANSLATION_ERROR_MESSAGE: ClassVar[str] = ""
    DEFAULT_PROMPT_PATH: ClassVar[Path | None] = None
    ABORT_ERROR_CLASS: ClassVar[type[MovieFieldTranslationTaskAbortError]] = (
        MovieFieldTranslationTaskAbortError
    )

    # === 通用配置,一般不用覆写 ===
    TRANSLATION_MAX_RETRIES = 3
    TRANSLATION_RETRY_DELAY_SECONDS = 5
    # 重试预算（决策 #9）：业务性失败按 30 分钟起步指数退避，5 次后 exhausted。
    TRANSLATION_RETRY_POLICY = RetryPolicy(
        max_attempts=5, backoff_base_seconds=1800, backoff_max_seconds=86400
    )

    def __init__(
        self,
        *,
        translation_client: MovieDescTranslationClient | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self.translation_client = translation_client or MovieDescTranslationClient()
        self.prompt_path = (
            Path(prompt_path) if prompt_path is not None else self.DEFAULT_PROMPT_PATH
        )

    # -------- 中断恢复 --------

    @classmethod
    def recover_interrupted_running_movies(
        cls, *, error_message: str | None = None
    ) -> int:
        normalized_error = (
            (error_message or "").strip() or cls.INTERRUPTED_TRANSLATION_ERROR_MESSAGE
        )
        return ResourceTaskLedger.recover_running(
            cls.TASK_KEY, error_message=normalized_error
        )

    # -------- 候选查询 --------

    @staticmethod
    def _subscribed_actor_exists_expression():
        # 用 EXISTS 判断影片是否关联已订阅女优,避免 join 放大结果集。
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
        # 翻译优先级固定为:已订阅影片 -> 订阅女优影片 -> 其他影片。
        return Case(
            None,
            (
                (Movie.is_subscribed == True, 0),
                (subscribed_actor_exists, 1),
            ),
            2,
        )

    @classmethod
    def _select_candidates(cls, state_condition, only_ids=None):
        source_field = getattr(Movie, cls.SOURCE_ATTR)
        target_field = getattr(Movie, cls.TARGET_ATTR)
        priority_order = cls._candidate_priority_expression()
        subscribed_time_is_null_order = Case(
            None,
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
        query = (
            # 只取 id：完整模型由内核分批加载，避免全量驻留内存（候选可达数十万）。
            Movie.select(Movie.id)
            .where(
                source_field != "",
                state_condition(cls.TASK_KEY, "movie", Movie.id),
            )
            .order_by(
                priority_order.asc(),
                subscribed_time_is_null_order.asc(),
                subscribed_time_order.asc(),
                non_subscribed_heat_order.desc(),
                Movie.id.desc(),
            )
        )
        if only_ids:
            # 手动指定即强制（统一 action 的 rerun 语义）：不看"是否已有译文"，
            # 只保留源字段非空；重译已完成影片就靠这条通路。
            query = query.where(Movie.id.in_(list(only_ids)))
        else:
            # 批跑只补缺：已有译文的影片不进候选。
            query = query.where(target_field == "")
        return query

    # -------- prompt / 输出规范化 --------

    def _load_prompt(self) -> str:
        if not self.prompt_path.exists():
            raise FileNotFoundError(
                f"{self.PROMPT_ERROR_PREFIX}_prompt_missing: {self.prompt_path}"
            )
        prompt_text = self.prompt_path.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise ValueError(
                f"{self.PROMPT_ERROR_PREFIX}_prompt_empty: {self.prompt_path}"
            )
        return prompt_text

    @staticmethod
    def _normalize_translated_text(translated_text: str) -> str:
        normalized_text = re.sub(
            r"<think>.*?</think>", "", translated_text, flags=re.DOTALL
        ).strip()
        # 这里处理的是当前 prompt 约定的业务语义,不是通用的 LLM 响应解析规则。
        if "无有效内容" in normalized_text:
            return ""
        return normalized_text

    # -------- 错误处理 --------

    @staticmethod
    def _normalize_error_message(exc: Exception) -> str:
        if isinstance(exc, MovieDescTranslationClientError):
            return exc.message
        return str(exc)

    @classmethod
    def _build_task_abort_message(
        cls, *, movie: Movie | None = None, detail: str
    ) -> str:
        normalized_detail = (
            (detail or "").strip() or cls.INTERRUPTED_TRANSLATION_ERROR_MESSAGE
        )
        if movie is None:
            return normalized_detail
        return (
            f"影片{cls.FIELD_LABEL}翻译任务中断 movie_number={movie.movie_number} "
            f"detail={normalized_detail}"
        )

    @staticmethod
    def _should_retry_then_abort_task(exc: MovieDescTranslationClientError) -> bool:
        return exc.should_retry_then_abort_task

    @classmethod
    def _retry_delay_seconds(cls, retry_index: int) -> int:
        # 翻译服务出现瞬时错误时按指数退避,避免短时间内持续打满上游。
        return cls.TRANSLATION_RETRY_DELAY_SECONDS * (2 ** retry_index)

    def _load_prompt_or_abort(self) -> str:
        try:
            return self._load_prompt()
        except Exception as exc:
            raise self.ABORT_ERROR_CLASS(
                self._build_task_abort_message(detail=str(exc))
            ) from exc

    # -------- 翻译调用 --------

    def _translate_with_retry(
        self, *, movie: Movie, system_prompt: str, source_text: str
    ) -> str:
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
                # 上游限流或服务异常时按指数退避重试,避免瞬时抖动直接终止整批任务。
                logger.warning(
                    "Movie {} translation retry movie_number={} retry_attempt={} "
                    "retry_delay_seconds={} status_code={} error_code={} detail={}",
                    self.LOG_LABEL,
                    movie.movie_number,
                    retry_index + 1,
                    retry_delay_seconds,
                    exc.status_code,
                    exc.error_code,
                    exc.message,
                )
                time.sleep(retry_delay_seconds)
        detail = self._normalize_error_message(
            last_exc or RuntimeError(self.INTERRUPTED_TRANSLATION_ERROR_MESSAGE)
        )
        raise self.ABORT_ERROR_CLASS(
            self._build_task_abort_message(movie=movie, detail=detail),
            status_code=last_exc.status_code if last_exc is not None else None,
            error_code=last_exc.error_code if last_exc is not None else None,
        )

    # -------- kernel 钩子 --------

    @classmethod
    def _apply_translation(cls, movie: Movie, translated_text: str) -> None:
        target_field = getattr(Movie, cls.TARGET_ATTR)
        setattr(movie, cls.TARGET_ATTR, translated_text)
        movie.save(only=[target_field])

    def _setup_run(self, _ctx):
        # prompt 缺失/为空属于任务级前置失败：中止整轮，不消耗任何影片的预算。
        try:
            return self._load_prompt()
        except Exception as exc:
            raise TaskAbortError(
                f"{self.PROMPT_ERROR_PREFIX}_prompt_unavailable", str(exc)
            ) from exc

    def _process_one(self, ctx, movie: Movie) -> None:
        latest_movie = Movie.get_by_id(movie.id)
        source_value = getattr(latest_movie, self.SOURCE_ATTR)
        target_value = getattr(latest_movie, self.TARGET_ATTR)
        if target_value:
            # 候选期后被其他链路补上译文：目标已达成，按成功收口。
            return
        if not source_value:
            raise TaskItemDeferred("source_missing", "原文已被清空，等待下轮重新筛选")
        try:
            translated_text = self._translate_with_retry(
                movie=latest_movie,
                system_prompt=ctx.shared,
                source_text=source_value,
            )
        except MovieFieldTranslationTaskAbortError as exc:
            raise TaskAbortError(
                exc.error_code or "translation_upstream_unavailable", exc.message
            ) from exc
        except MovieDescTranslationClientError as exc:
            raise TaskItemError(
                exc.error_code or "translation_rejected", exc.message
            ) from exc
        self._apply_translation(
            latest_movie, self._normalize_translated_text(translated_text)
        )

    # -------- 批量入口 --------

    def run(self, *, reporter, only_ids: list[int] | None = None) -> dict[str, int]:
        if not settings.movie_info_translation.enabled:
            disabled_stats = {
                "candidate_movies": 0,
                "processed_movies": 0,
                "succeeded_movies": 0,
                "failed_movies": 0,
                "updated_movies": 0,
                "skipped_movies": 0,
            }
            reporter.emit(
                current=0,
                total=0,
                text=f"影片{self.FIELD_LABEL}翻译未启用,跳过执行",
                summary_patch=disabled_stats,
            )
            return disabled_stats

        spec = ResourceTaskSpec(
            task_key=self.TASK_KEY,
            resource_type="movie",
            retry=self.TRANSLATION_RETRY_POLICY,
            select_candidates=self._select_candidates,
            process_one=self._process_one,
            setup_run=self._setup_run,
            resource_model=Movie,
        )
        stats = ResourceTaskRunner.run(spec, reporter, only_ids=only_ids)
        # 保持旧统计口径（registry format_stats 与日志锚点不变）。
        return {
            "candidate_movies": stats["candidate_count"],
            "processed_movies": stats["candidate_count"] - stats["deferred_count"],
            "succeeded_movies": stats["succeeded_count"],
            "failed_movies": (
                stats["failed_retryable_count"]
                + stats["failed_terminal_count"]
                + stats["exhausted_count"]
            ),
            "updated_movies": stats["succeeded_count"],
            "skipped_movies": stats["deferred_count"],
        }
