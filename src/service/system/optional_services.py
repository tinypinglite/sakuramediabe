"""可选向量服务的有效能力；只读启动配置，不做网络探测。"""
from src.api.exception.errors import ApiError
from src.config.config import settings


def movie_similarity_enabled() -> bool:
    return settings.qdrant.enabled


def image_search_enabled() -> bool:
    """图搜依赖 Qdrant，两个开关都开启时才提供该能力。"""
    return movie_similarity_enabled() and settings.image_search.enabled


def capabilities() -> dict[str, bool]:
    return {
        "movie_similarity": movie_similarity_enabled(),
        "image_search": image_search_enabled(),
    }


def require_image_search() -> None:
    if not image_search_enabled():
        raise ApiError(409, "feature_disabled", "当前服务器未启用图片与文字搜图")


def job_disabled_reason(task_key: str) -> str | None:
    if task_key == "image_search_index" and not image_search_enabled():
        return "图片与文字搜图未启用"
    if task_key == "movie_similarity_recompute" and not movie_similarity_enabled():
        return "相似影片与向量服务未启用"
    return None


def require_job_enabled(task_key: str) -> None:
    reason = job_disabled_reason(task_key)
    if reason:
        raise ApiError(409, "feature_disabled", reason)
