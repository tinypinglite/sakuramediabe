#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

# 允许从仓库根目录直接运行脚本：poetry run python scripts/...
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 固定检查参数：脚本为零业务参数入口，不从命令行接收业务值。
DEFAULT_PERIOD = "weekly"
DEFAULT_PAGE = 1
DEFAULT_LIMIT = 24
DEFAULT_MOVIE_SORT_BY = "recently"

REQUIRED_REVIEW_RESOURCE_FIELDS = (
    "id",
    "score",
    "content",
    "created_at",
    "username",
    "like_count",
    "watch_count",
)
REQUIRED_REVIEW_MOVIE_FIELDS = (
    "id",
    "number",
    "title",
    "origin_title",
    "score",
    "thumb_url",
    "release_date",
)


def _username_hash(value: Any) -> str:
    raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _contains_sensitive_link(content: Any) -> bool:
    text = str(content or "").lower()
    return "magnet:?" in text or "ed2k://" in text


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _coverage(items: List[Any], required_fields: List[str], field_getter) -> Dict[str, float]:
    if not items:
        return {field: 0.0 for field in required_fields}
    count = len(items)
    return {
        field: round(sum(1 for item in items if _is_present(field_getter(item, field))) / count, 3)
        for field in required_fields
    }


def _sanitize_reviews_preview(reviews: List[Any], max_items: int = 3) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for review in reviews[:max_items]:
        movie = getattr(review, "movie", None)
        preview.append(
            {
                "id": getattr(review, "id", None),
                "score": getattr(review, "score", None),
                "content_len": len(str(getattr(review, "content", "") or "")),
                "contains_sensitive_link": _contains_sensitive_link(getattr(review, "content", "")),
                "username_hash": _username_hash(getattr(review, "username", "")),
                "movie_number": getattr(movie, "number", None) if movie is not None else None,
            }
        )
    return preview


def _validate_review_resources(reviews: List[Any], endpoint_name: str) -> Dict[str, Any]:
    _ensure(isinstance(reviews, list), f"[{endpoint_name}] 返回值不是 list")
    _ensure(len(reviews) > 0, f"[{endpoint_name}] 返回空列表，无法验证契约")

    movie_items = []
    for review in reviews:
        for field in REQUIRED_REVIEW_RESOURCE_FIELDS:
            _ensure(hasattr(review, field), f"[{endpoint_name}] 评论缺少字段: {field}")
        _ensure(isinstance(review.id, int), f"[{endpoint_name}] review.id 不是 int")
        _ensure(isinstance(review.score, int), f"[{endpoint_name}] review.score 不是 int")
        _ensure(isinstance(review.content, str), f"[{endpoint_name}] review.content 不是 str")
        _ensure(
            review.created_at is None or isinstance(review.created_at, str),
            f"[{endpoint_name}] review.created_at 类型非法",
        )
        _ensure(isinstance(review.username, str), f"[{endpoint_name}] review.username 不是 str")
        _ensure(isinstance(review.like_count, int), f"[{endpoint_name}] review.like_count 不是 int")
        _ensure(isinstance(review.watch_count, int), f"[{endpoint_name}] review.watch_count 不是 int")

        movie = getattr(review, "movie", None)
        if movie is None:
            continue
        for field in REQUIRED_REVIEW_MOVIE_FIELDS:
            _ensure(hasattr(movie, field), f"[{endpoint_name}] review.movie 缺少字段: {field}")
        _ensure(isinstance(movie.id, str), f"[{endpoint_name}] movie.id 不是 str")
        _ensure(isinstance(movie.number, str), f"[{endpoint_name}] movie.number 不是 str")
        _ensure(isinstance(movie.title, str), f"[{endpoint_name}] movie.title 不是 str")
        _ensure(movie.origin_title is None or isinstance(movie.origin_title, str), f"[{endpoint_name}] movie.origin_title 类型非法")
        _ensure(movie.score is None or isinstance(movie.score, float), f"[{endpoint_name}] movie.score 不是 float/None")
        _ensure(movie.thumb_url is None or isinstance(movie.thumb_url, str), f"[{endpoint_name}] movie.thumb_url 类型非法")
        _ensure(
            movie.release_date is None or isinstance(movie.release_date, str),
            f"[{endpoint_name}] movie.release_date 类型非法",
        )
        movie_items.append(movie)

    def _review_getter(item: Any, field: str):
        return getattr(item, field, None)

    def _movie_getter(item: Any, field: str):
        return getattr(item, field, None)

    return {
        "reviews_count": len(reviews),
        "review_field_coverage": _coverage(reviews, list(REQUIRED_REVIEW_RESOURCE_FIELDS), _review_getter),
        "movie_count": len(movie_items),
        "movie_field_coverage": _coverage(movie_items, list(REQUIRED_REVIEW_MOVIE_FIELDS), _movie_getter),
        "preview": _sanitize_reviews_preview(reviews),
    }


def _validate_movie_detail_resource(detail: Any, endpoint_name: str) -> Dict[str, Any]:
    _ensure(isinstance(detail.javdb_id, str) and detail.javdb_id, f"[{endpoint_name}] javdb_id 非法")
    _ensure(isinstance(detail.movie_number, str) and detail.movie_number, f"[{endpoint_name}] movie_number 非法")
    _ensure(isinstance(detail.title, str), f"[{endpoint_name}] title 类型非法")
    _ensure(isinstance(detail.actors, list), f"[{endpoint_name}] actors 不是 list")
    _ensure(isinstance(detail.tags, list), f"[{endpoint_name}] tags 不是 list")
    _ensure(isinstance(detail.plot_images, list), f"[{endpoint_name}] plot_images 不是 list")

    actor_count = 0
    for actor in detail.actors:
        _ensure(isinstance(actor.javdb_id, str) and actor.javdb_id, f"[{endpoint_name}] actor.javdb_id 非法")
        _ensure(isinstance(actor.name, str), f"[{endpoint_name}] actor.name 类型非法")
        _ensure(isinstance(actor.javdb_type, int), f"[{endpoint_name}] actor.javdb_type 类型非法")
        _ensure(isinstance(actor.gender, int), f"[{endpoint_name}] actor.gender 类型非法")
        actor_count += 1

    return {
        "javdb_id": detail.javdb_id,
        "movie_number": detail.movie_number,
        "actor_count": actor_count,
        "tag_count": len(detail.tags),
        "plot_image_count": len(detail.plot_images),
    }


def _validate_actor_resource(actor: Any, endpoint_name: str) -> Dict[str, Any]:
    _ensure(isinstance(actor.javdb_id, str) and actor.javdb_id, f"[{endpoint_name}] actor.javdb_id 非法")
    _ensure(isinstance(actor.name, str) and actor.name.strip(), f"[{endpoint_name}] actor.name 非法")
    _ensure(isinstance(actor.javdb_type, int), f"[{endpoint_name}] actor.javdb_type 类型非法")
    _ensure(isinstance(actor.gender, int), f"[{endpoint_name}] actor.gender 类型非法")
    _ensure(actor.avatar_url is None or isinstance(actor.avatar_url, str), f"[{endpoint_name}] actor.avatar_url 类型非法")
    return {
        "actor_javdb_id": actor.javdb_id,
        "actor_name_hash": _username_hash(actor.name),
        "actor_type": actor.javdb_type,
    }


def _run_check(name: str, fn, checks: List[Dict[str, Any]]) -> Optional[Any]:
    try:
        detail = fn()
        if isinstance(detail, dict):
            checks.append({"name": name, "status": "pass", **detail})
        else:
            checks.append({"name": name, "status": "pass"})
        return detail
    except Exception as exc:
        checks.append({"name": name, "status": "fail", "reason": str(exc)})
        return None


def _configure_logging() -> None:
    # live 校验只输出脱敏报告，屏蔽 provider 内部 debug payload 日志。
    logger.remove()
    logger.add(sys.stderr, level="ERROR")


def _discover_sample_from_hot_reviews(provider: Any, hot_reviews: List[Any]) -> Dict[str, Any]:
    # 先从热评里自动找可用电影，再逐个尝试补齐演员链路样本。
    candidates: List[Dict[str, str]] = []
    seen: set[str] = set()
    for review in hot_reviews:
        movie = getattr(review, "movie", None)
        if movie is None:
            continue
        movie_id = movie.id.strip()
        movie_number = movie.number.strip()
        if not movie_id or not movie_number:
            continue
        if movie_id in seen:
            continue
        seen.add(movie_id)
        candidates.append({"movie_id": movie_id, "movie_number": movie_number})

    _ensure(candidates, "hot reviews 中没有可用于样本发现的 movie.id/movie.number")

    errors: List[str] = []
    for candidate in candidates:
        movie_id = candidate["movie_id"]
        movie_number = candidate["movie_number"]
        try:
            detail = provider.get_movie_by_javdb_id(movie_id)
            actor_name = None
            for actor in detail.actors:
                if isinstance(actor.name, str) and actor.name.strip():
                    actor_name = actor.name.strip()
                    break
            _ensure(actor_name is not None, f"movie={movie_number} 无可用演员名")
            actor_resource = provider.search_actor(actor_name)
            return {
                "movie_id": movie_id,
                "movie_number": movie_number,
                "actor_name": actor_name,
                "actor_javdb_id": actor_resource.javdb_id,
                "actor_type": actor_resource.javdb_type,
            }
        except (AssertionError, Exception) as exc:
            errors.append(f"{movie_number}:{exc}")
            continue

    raise AssertionError("无法从热评候选里发现可覆盖电影+演员链路的样本: " + "; ".join(errors[:5]))


def main() -> int:
    _configure_logging()
    from src.metadata.javdb import JavdbProvider
    from src.config.config import settings

    provider = JavdbProvider(
        host=settings.metadata.javdb_host,
        # live 契约校验仅验证 JavDB 链路，JavDB 始终走直连。
        proxy=None,
    )
    checks: List[Dict[str, Any]] = []

    hot_reviews_result = _run_check(
        "get_hot_reviews",
        lambda: _validate_review_resources(
            provider.get_hot_reviews(
                period=DEFAULT_PERIOD,
                page=DEFAULT_PAGE,
                limit=DEFAULT_LIMIT,
            ),
            endpoint_name="get_hot_reviews",
        ),
        checks,
    )

    sample: Optional[Dict[str, Any]] = None
    if hot_reviews_result is not None:
        hot_reviews = provider.get_hot_reviews(
            period=DEFAULT_PERIOD,
            page=DEFAULT_PAGE,
            limit=DEFAULT_LIMIT,
        )
        sample = _run_check(
            "sample_discovery",
            lambda: _discover_sample_from_hot_reviews(provider, hot_reviews),
            checks,
        )
    else:
        checks.append(
            {
                "name": "sample_discovery",
                "status": "fail",
                "reason": "依赖 get_hot_reviews 成功",
            }
        )

    if sample is not None:
        movie_id = sample["movie_id"]
        movie_number = sample["movie_number"]
        actor_name = sample["actor_name"]
        actor_javdb_id = sample["actor_javdb_id"]
        actor_type = sample["actor_type"]

        _run_check(
            "get_movie_reviews_by_javdb_id",
            lambda: _validate_review_resources(
                provider.get_movie_reviews_by_javdb_id(
                    movie_id,
                    page=DEFAULT_PAGE,
                    limit=DEFAULT_LIMIT,
                    sort_by=DEFAULT_MOVIE_SORT_BY,
                ),
                endpoint_name="get_movie_reviews_by_javdb_id",
            ),
            checks,
        )

        detail_by_id_holder: Dict[str, Any] = {}
        detail_by_number_holder: Dict[str, Any] = {}
        detail_alias_holder: Dict[str, Any] = {}

        def _check_movie_by_id():
            detail = provider.get_movie_by_javdb_id(movie_id)
            detail_by_id_holder["value"] = detail
            return _validate_movie_detail_resource(detail, "get_movie_by_javdb_id")

        def _check_movie_by_number():
            detail = provider.get_movie_by_number(movie_number)
            detail_by_number_holder["value"] = detail
            return _validate_movie_detail_resource(detail, "get_movie_by_number")

        def _check_movie_detail_alias():
            detail = provider.get_movie_detail(movie_number)
            detail_alias_holder["value"] = detail
            return _validate_movie_detail_resource(detail, "get_movie_detail")

        _run_check("get_movie_by_javdb_id", _check_movie_by_id, checks)
        _run_check("get_movie_by_number", _check_movie_by_number, checks)
        _run_check("get_movie_detail", _check_movie_detail_alias, checks)

        def _check_movie_consistency():
            detail_by_id = detail_by_id_holder.get("value")
            detail_by_number = detail_by_number_holder.get("value")
            detail_alias = detail_alias_holder.get("value")
            _ensure(detail_by_id is not None, "缺少 get_movie_by_javdb_id 结果")
            _ensure(detail_by_number is not None, "缺少 get_movie_by_number 结果")
            _ensure(detail_alias is not None, "缺少 get_movie_detail 结果")
            _ensure(
                detail_by_number.javdb_id == detail_alias.javdb_id,
                "get_movie_by_number 与 get_movie_detail 的 javdb_id 不一致",
            )
            return {
                "javdb_id_by_number": detail_by_number.javdb_id,
                "javdb_id_by_detail": detail_alias.javdb_id,
            }

        _run_check("movie_consistency", _check_movie_consistency, checks)

        actor_holder: Dict[str, Any] = {}
        actors_holder: Dict[str, Any] = {}

        def _check_search_actor():
            actor = provider.search_actor(actor_name)
            actor_holder["value"] = actor
            return _validate_actor_resource(actor, "search_actor")

        def _check_search_actors():
            actors = provider.search_actors(actor_name)
            _ensure(isinstance(actors, list) and actors, "search_actors 返回空列表")
            for actor in actors:
                _validate_actor_resource(actor, "search_actors")
            actors_holder["value"] = actors
            return {
                "actors_count": len(actors),
            }

        _run_check("search_actor", _check_search_actor, checks)
        _run_check("search_actors", _check_search_actors, checks)

        def _check_actor_search_consistency():
            actor = actor_holder.get("value")
            actors = actors_holder.get("value") or []
            _ensure(actor is not None, "缺少 search_actor 结果")
            actor_ids = {item.javdb_id for item in actors}
            _ensure(actor.javdb_id in actor_ids, "search_actor 结果不在 search_actors 结果集中")
            return {
                "actor_javdb_id": actor.javdb_id,
                "actors_count": len(actors),
            }

        _run_check("actor_search_consistency", _check_actor_search_consistency, checks)

        def _check_get_actor_movies():
            movies = provider.get_actor_movies(actor_name, page=DEFAULT_PAGE)
            _ensure(isinstance(movies, list), "get_actor_movies 返回值不是 list")
            for movie in movies[:5]:
                _ensure(isinstance(movie.javdb_id, str) and movie.javdb_id, "get_actor_movies 中 movie.javdb_id 非法")
                _ensure(isinstance(movie.movie_number, str), "get_actor_movies 中 movie.movie_number 类型非法")
                _ensure(isinstance(movie.title, str), "get_actor_movies 中 movie.title 类型非法")
            return {"movies_count": len(movies)}

        def _check_get_actor_movies_by_javdb():
            movies = provider.get_actor_movies_by_javdb(
                actor_javdb_id=actor_javdb_id,
                actor_type=actor_type,
                page=DEFAULT_PAGE,
            )
            _ensure(isinstance(movies, list), "get_actor_movies_by_javdb 返回值不是 list")
            for movie in movies[:5]:
                _ensure(isinstance(movie.javdb_id, str) and movie.javdb_id, "get_actor_movies_by_javdb 中 movie.javdb_id 非法")
                _ensure(isinstance(movie.movie_number, str), "get_actor_movies_by_javdb 中 movie.movie_number 类型非法")
                _ensure(isinstance(movie.title, str), "get_actor_movies_by_javdb 中 movie.title 类型非法")
            return {"movies_count": len(movies), "actor_type": actor_type}

        _run_check("get_actor_movies", _check_get_actor_movies, checks)
        _run_check("get_actor_movies_by_javdb", _check_get_actor_movies_by_javdb, checks)
    else:
        dependent_checks = [
            "get_movie_reviews_by_javdb_id",
            "get_movie_by_javdb_id",
            "get_movie_by_number",
            "get_movie_detail",
            "movie_consistency",
            "search_actor",
            "search_actors",
            "actor_search_consistency",
            "get_actor_movies",
            "get_actor_movies_by_javdb",
        ]
        for name in dependent_checks:
            checks.append({"name": name, "status": "fail", "reason": "依赖 sample_discovery 成功"})

    def _check_rank_numbers():
        matrix: List[Dict[str, Any]] = []
        for video_type in sorted(provider.SUPPORTED_RANK_VIDEO_TYPES):
            for period in sorted(provider.SUPPORTED_RANK_PERIODS):
                numbers = provider.get_rank_numbers(video_type=video_type, period=period)
                _ensure(isinstance(numbers, list), f"rank numbers 结果不是 list: {video_type}/{period}")
                for number in numbers:
                    _ensure(isinstance(number, str), f"rank number 元素不是 str: {video_type}/{period}")
                matrix.append(
                    {
                        "video_type": video_type,
                        "period": period,
                        "count": len(numbers),
                    }
                )
        return {"matrix": matrix}

    _run_check("get_rank_numbers_matrix", _check_rank_numbers, checks)

    failed_count = sum(1 for check in checks if check.get("status") == "fail")
    passed_count = len(checks) - failed_count

    output = {
        "status": "ok" if failed_count == 0 else "failed",
        "config": {
            "host": settings.metadata.javdb_host,
            "javdb_proxy_enabled": False,
        },
        "defaults": {
            "period": DEFAULT_PERIOD,
            "page": DEFAULT_PAGE,
            "limit": DEFAULT_LIMIT,
            "movie_sort_by": DEFAULT_MOVIE_SORT_BY,
        },
        "summary": {
            "total": len(checks),
            "passed": passed_count,
            "failed": failed_count,
        },
        "checks": checks,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
