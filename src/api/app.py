import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.exception.errors import ApiError
from src.api.exception.exception import (
    all_exception_handler,
    api_error_handler,
    http_exception_handler,
    validation_exception_handler,
)
from src.api.routers.catalog.actors import router as actors_router
from src.api.routers.catalog.movies import router as movies_router
from src.api.routers.catalog.subscriptions import router as movie_subscriptions_router
from src.api.routers.catalog.subtitle_imports import router as subtitle_imports_router
from src.api.routers.catalog.tags import router as tags_router
from src.api.routers.collections.clip_collections import (
    router as clip_collections_router,
)
from src.api.routers.collections.playlists import router as playlists_router
from src.api.routers.discovery.daily_recommendations import (
    router as daily_recommendations_router,
)
from src.api.routers.discovery.hot_reviews import router as hot_reviews_router
from src.api.routers.discovery.image_search import router as image_search_router
from src.api.routers.discovery.moment_recommendations import (
    router as moment_recommendations_router,
)
from src.api.routers.discovery.ranking_sources import router as ranking_sources_router
from src.api.routers.files.images import router as file_images_router
from src.api.routers.files.subtitles import router as file_subtitles_router
from src.api.routers.playback.cloud115_libraries import (
    router as cloud115_libraries_router,
)
from src.api.routers.playback.media import router as media_router
from src.api.routers.playback.media_clips import router as media_clips_router
from src.api.routers.playback.media_libraries import router as media_libraries_router
from src.api.routers.playback.media_points import router as media_points_router
from src.api.routers.system.account import router as account_router
from src.api.routers.system.activity import router as activity_router
from src.api.routers.system.auth import router as auth_router
from src.api.routers.system.config import router as config_router
from src.api.routers.system.indexer_settings import router as indexer_settings_router
from src.api.routers.system.jobs import router as jobs_router
from src.api.routers.system.plugins import router as plugins_router
from src.api.routers.system.status import router as status_router
from src.api.routers.transfers.downloads import router as downloads_router
from src.api.routers.transfers.media_import import router as media_import_router
from src.api.routers.transfers.rapid_uploads import router as rapid_uploads_router
from src.api.routers.videos.collections import router as video_collections_router
from src.api.routers.videos.items import router as videos_router
from src.common.database import ensure_database_ready
from src.common.logging import configure_logging
from src.config.config import ensure_runtime_config, settings


def _create_lifespan():
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger(__name__).info("Starting FastAPI runtime jobs")
        # 服务对外前确保运行配置就绪：缺失/空文件写入全量默认配置，并自举鉴权密钥落盘。
        ensure_runtime_config()
        ensure_database_ready()
        yield

    return lifespan


def create_app() -> FastAPI:
    configure_logging()
    if settings.enable_docs:
        app = FastAPI(lifespan=_create_lifespan())
    else:
        app = FastAPI(docs_url=None, redoc_url=None, lifespan=_create_lifespan())

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(actors_router)
    app.include_router(movies_router)
    app.include_router(movie_subscriptions_router)
    app.include_router(subtitle_imports_router)
    app.include_router(tags_router)
    app.include_router(playlists_router)
    app.include_router(clip_collections_router)
    app.include_router(file_images_router)
    app.include_router(file_subtitles_router)
    # 固定子路径必须先于 /media/{media_id} 动态路由注册。
    app.include_router(rapid_uploads_router)
    app.include_router(media_router)
    app.include_router(media_clips_router)
    app.include_router(media_points_router)
    app.include_router(media_libraries_router)
    app.include_router(cloud115_libraries_router)
    app.include_router(daily_recommendations_router)
    app.include_router(image_search_router)
    app.include_router(moment_recommendations_router)
    app.include_router(hot_reviews_router)
    app.include_router(ranking_sources_router)
    app.include_router(downloads_router)
    app.include_router(media_import_router)
    app.include_router(status_router)
    app.include_router(activity_router)
    app.include_router(jobs_router)
    app.include_router(account_router)
    app.include_router(auth_router)
    app.include_router(indexer_settings_router)
    app.include_router(plugins_router)

    app.include_router(config_router)

    app.include_router(videos_router)
    app.include_router(video_collections_router)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, all_exception_handler)
    return app


app = create_app()
