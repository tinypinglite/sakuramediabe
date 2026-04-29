#!/usr/bin/env python
# -*- coding: utf-8 -*-

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.common.logging import configure_logging
from src.api.exception.exception import (
    all_exception_handler,
    api_error_handler,
    http_exception_handler,
    validation_exception_handler,
)
from src.api.routers.collections.playlists import router as playlists_router
from src.api.routers.files.images import router as file_images_router
from src.api.routers.files.subtitles import router as file_subtitles_router
from src.api.routers.playback.media import router as media_router
from src.api.routers.playback.media_points import router as media_points_router
from src.api.routers.playback.media_libraries import router as media_libraries_router
from src.api.routers.transfers.downloads import router as downloads_router
from src.api.exception.errors import ApiError
from src.api.routers.catalog.actors import router as actors_router
from src.api.routers.catalog.movies import router as movies_router
from src.api.routers.catalog.tags import router as tags_router
from src.api.routers.discovery.hot_reviews import router as hot_reviews_router
from src.api.routers.discovery.image_search import router as image_search_router
from src.api.routers.discovery.ranking_sources import router as ranking_sources_router
from src.api.routers.system.account import router as account_router
from src.api.routers.system.auth import router as auth_router
from src.api.routers.system.activity import router as activity_router
from src.api.routers.system.collection_number_features import router as collection_number_features_router
from src.api.routers.system.indexer_settings import router as indexer_settings_router
from src.api.routers.system.movie_desc_translation_settings import (
    router as movie_desc_translation_settings_router,
)
from src.api.routers.system.metadata_provider_license import (
    router as metadata_provider_license_router,
)
from src.api.routers.system.status import router as status_router
from src.common.database import ensure_database_ready
from src.config.config import settings
from src.start.recovery import recover_interrupted_tasks


def _create_lifespan():
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logging.getLogger(__name__).info("Starting FastAPI runtime jobs")
        ensure_database_ready()
        # 容器入口已经在启动前完成 schema 升级，这里只负责运行时恢复逻辑。
        recover_interrupted_tasks(
            trigger_types=("startup", "manual", "internal"),
            error_message="API进程重启，任务已中断",
        )
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
    app.include_router(tags_router)
    app.include_router(playlists_router)
    app.include_router(file_images_router)
    app.include_router(file_subtitles_router)
    app.include_router(media_router)
    app.include_router(media_points_router)
    app.include_router(media_libraries_router)
    app.include_router(image_search_router)
    app.include_router(hot_reviews_router)
    app.include_router(ranking_sources_router)
    app.include_router(downloads_router)
    app.include_router(status_router)
    app.include_router(activity_router)
    app.include_router(account_router)
    app.include_router(auth_router)
    app.include_router(indexer_settings_router)
    app.include_router(movie_desc_translation_settings_router)
    app.include_router(metadata_provider_license_router)
    app.include_router(collection_number_features_router)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, all_exception_handler)
    return app


app = create_app()
