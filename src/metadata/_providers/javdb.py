from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

from loguru import logger

from src.common.service_helpers import safe_int

from .exceptions import JavdbAuthError, MetadataNotFoundError, MetadataRequestError
from .http_client import MetadataRequestClient
from .models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
    JavdbMovieReviewResource,
    JavdbMovieTagResource,
    JavdbReviewMovieResource,
    JavdbSeriesResource,
)
from .utils import normalize_movie_number


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


class JavdbProvider(MetadataRequestClient):
    SUPPORTED_RANK_VIDEO_TYPES = {"0", "1", "3"}
    SUPPORTED_RANK_PERIODS = {"daily", "weekly", "monthly"}
    # 播放榜筛选：all=热播，high_score=高评分。
    SUPPORTED_PLAYBACK_FILTERS = {"all", "high_score"}
    SUPPORTED_HOT_REVIEW_PERIODS = {
        "weekly", "all", "quarterly", "monthly", "yearly"}
    API_PATH_MOVIES_TAGS = "/api/v1/movies/tags"
    API_PATH_SEARCH = "/api/v2/search"
    API_PATH_MOVIE_DETAIL = "/api/v4/movies/{javdb_id}"
    API_PATH_MOVIE_REVIEWS = "/api/v1/movies/{javdb_id}/reviews"
    API_PATH_HOT_REVIEWS = "/api/v1/reviews/hotly"
    API_PATH_RANKINGS = "/api/v1/rankings"
    API_PATH_RANKINGS_PLAYBACK = "/api/v1/rankings/playback"
    API_PATH_SESSIONS = "/api/v1/sessions"
    API_PATH_MOVIES_TOP = "/api/v1/movies/top"

    # TOP250：每页 50 条，5 页满 250。
    TOP250_PAGE_LIMIT = 50
    TOP250_MAX_PAGES = 5
    SUPPORTED_TOP_TYPES = {"all", "year", "video_type"}

    # 登录所需的固定设备指纹，与官方 App 抓包一致，不要改动。
    DEVICE = {
        "device_name": "meizu16sPro",
        "device_model": "meizu/16s Pro",
        "platform": "android",
        "system_version": "9",
        "app_channel": "official",
        "app_version": "official",
        "app_version_number": "1.9.29",
    }
    # 派生 device_uuid 的固定命名空间（沿用原硬编码值作为种子）：
    # device_uuid 按账号 uuid5 派生，做到同账号稳定、跨账号唯一，避免全网共用一个设备标识。
    DEVICE_UUID_NAMESPACE = uuid.UUID("5374dc5e-0f98-5235-9845-76cbc0ced71f")

    API_PARAMS_ACTOR_MOVIES = {
        "sort_by": "release",
        "order_by": "desc",
    }
    API_PARAMS_SERIES_MOVIES = {
        "sort_by": "release",
        "order_by": "desc",
    }
    API_PARAMS_ACTOR_SEARCH = {
        "from_recent": "false",
        "type": "actor",
        "page": 1,
        "limit": 24,
    }
    API_PARAMS_SERIES_SEARCH = {
        "from_recent": "false",
        "type": "series",
        "page": 1,
        "limit": 24,
    }
    API_PARAMS_MOVIE_SEARCH = {
        "from_recent": "false",
        "type": "movie",
        "movie_type": "all",
        "movie_sort_by": "relevance",
        "movie_filter_by": "all",
        "page": 1,
        "limit": 24,
    }
    API_PARAMS_MOVIE_DETAIL = {
        "from_rankings": "true",
    }
    API_PARAMS_MOVIE_REVIEWS = {
        "page": 1,
        "limit": 20,
    }
    API_PARAMS_HOT_REVIEWS = {
        "period": "weekly",
        "page": 1,
        "limit": 24,
    }

    def __init__(
        self,
        host: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ):
        MetadataRequestClient.__init__(self)
        self.host = host
        self.username = username
        self.password = password
        # 登录态懒加载：token 在首次抓需登录榜单时获取，本实例只登录一次。
        self._token: str | None = None
        self._login_failed = False
        logger.info(
            "JavdbProvider initialized host={} account_configured={}",
            host,
            bool(username and password),
        )

    def _normalize_image_url(self, url: str | None) -> str | None:
        if not url:
            return None
        if "covers" in url:
            return f"https://c0.jdbstatic.com/covers/{url.split('covers/')[-1]}"
        if "samples" in url:
            return f"https://c0.jdbstatic.com/samples/{url.split('samples/')[-1]}"
        if "avatars" in url:
            return f"https://c0.jdbstatic.com/avatars/{url.split('avatars/')[-1]}"
        return url

    def _build_api_url(
        self,
        *,
        path: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> str:
        resolved_path = path.format(**(path_params or {}))
        base_url = f"https://{self.host}{resolved_path}"
        if not query_params:
            return base_url
        return f"{base_url}?{urlencode(query_params, quote_via=quote, safe=':-')}"

    def get_actor_movies(
        self, actor_name: str, page: int = 1
    ) -> list[JavdbMovieListItemResource]:
        logger.debug(
            "Javdb get_actor_movies start actor_name={} page={}", actor_name, page)
        actor_info = self._search_actor(actor_name)
        return self.get_actor_movies_by_javdb(
            actor_javdb_id=actor_info["id"],
            actor_type=actor_info["type"],
            page=page,
        )

    def get_actor_movies_by_javdb(
        self,
        actor_javdb_id: str,
        actor_type: int = 0,
        page: int = 1,
    ) -> list[JavdbMovieListItemResource]:
        logger.debug(
            "Javdb get_actor_movies_by_javdb start actor_javdb_id={} actor_type={} page={}",
            actor_javdb_id,
            actor_type,
            page,
        )
        query_params = {
            "filter_by": f"{actor_type}:a:{actor_javdb_id}",
            **self.API_PARAMS_ACTOR_MOVIES,
            "page": page,
        }
        url = self._build_api_url(
            path=self.API_PATH_MOVIES_TAGS,
            query_params=query_params,
        )
        payload = self.request_json("GET", url)
        data = payload.get("data", {})
        movies = []
        for movie in data.get("movies", []):
            movies.append(self._build_movie_list_item(movie))
        logger.debug(
            "Javdb get_actor_movies_by_javdb success actor_javdb_id={} page={} movies={}",
            actor_javdb_id,
            page,
            len(movies),
        )
        return movies

    def get_series_movies(
        self,
        series_id: str,
        series_type: int = 0,
    ) -> list[JavdbMovieListItemResource]:
        page = 1
        movies = []
        while True:
            logger.debug(
                "Javdb get_series_movies start series_id={} page={} series_type={}",
                series_id,
                page,
                series_type,
            )
            query_params = {
                "filter_by": f"{series_type}:s:{series_id}",
                **self.API_PARAMS_SERIES_MOVIES,
                "page": page,
            }
            url = self._build_api_url(
                path=self.API_PATH_MOVIES_TAGS,
                query_params=query_params,
            )
            payload = self.request_json("GET", url)
            data = payload.get("data", {})
            _movies = data.get("movies", [])
            if not _movies:
                break
            for movie in _movies:
                movies.append(self._build_movie_list_item(movie))
            page += 1

        return movies

    def search_series(
        self,
        series_name: str,
        page: int = 1,
    ) -> list[JavdbSeriesResource]:
        query_params = {
            "q": series_name,
            **self.API_PARAMS_SERIES_SEARCH,
            "page": page,
        }
        url = self._build_api_url(
            path=self.API_PATH_SEARCH,
            query_params=query_params,
        )
        logger.debug("Javdb search_series query={} page={} url={}", series_name, page, url)
        payload = self.request_json("GET", url)
        series_list = payload.get("data", {}).get("series", [])
        logger.debug(
            "Javdb search_series candidates series_name={} count={}",
            series_name,
            len(series_list),
        )
        resources: list[JavdbSeriesResource] = []
        for series in series_list:
            series_id = series.get("id")
            if not series_id:
                continue
            resources.append(
                JavdbSeriesResource.model_validate(
                    {
                        "javdb_id": series_id,
                        "javdb_type": series.get("type") or 0,
                        "name": series.get("name") or "",
                        "videos_count": series.get("videos_count") or 0,
                    }
                )
            )
        logger.debug(
            "Javdb search_series success series_name={} count={}",
            series_name,
            len(resources),
        )
        return resources

    def _search_actor(self, actor_name: str) -> dict:
        query_params = {
            "q": actor_name,
            **self.API_PARAMS_ACTOR_SEARCH,
        }
        url = self._build_api_url(
            path=self.API_PATH_SEARCH,
            query_params=query_params,
        )
        logger.debug("Javdb search actor query={} url={}", actor_name, url)
        payload = self.request_json("GET", url)
        actors = payload.get("data", {}).get("actors", [])
        logger.debug(
            "Javdb search actor candidates actor_name={} count={}", actor_name, len(actors))
        if not actors:
            logger.warning("Javdb actor not found actor_name={}", actor_name)
            raise MetadataNotFoundError("actor", actor_name)
        target_actor = None
        for actor in actors:
            if actor.get("name", "") == actor_name:
                target_actor = actor
                break

            if actor.get('name_zht') == actor_name:
                target_actor = actor
                break
            if actor.get('other_name'):
                for name in actor.get('other_name').split(','):
                    if name == actor_name:
                        target_actor = actor
                        break
                if target_actor is not None:
                    break
        if target_actor:
            raw_gender = target_actor.get("gender")
            if raw_gender is None:
                gender = 0
            else:
                gender = int(not raw_gender)
            logger.debug(
                "Javdb actor matched actor_name={} actor_id={} actor_type={}",
                actor_name,
                target_actor["id"],
                target_actor["type"],
            )
            return {
                "id": target_actor["id"],
                "type": target_actor["type"],
                "name": target_actor.get("name") or actor_name,
                "alias_names": self._collect_actor_candidate_names(target_actor),
                "avatar_url": self._resolve_actor_avatar_url(target_actor),
                "gender": gender,
            }
        logger.warning(
            "Javdb actor not matched in candidate list actor_name={}", actor_name)
        raise MetadataNotFoundError("actor", actor_name)

    def search_actors(self, actor_name: str) -> list[JavdbMovieActorResource]:
        query_params = {
            "q": actor_name,
            **self.API_PARAMS_ACTOR_SEARCH,
        }
        url = self._build_api_url(
            path=self.API_PATH_SEARCH,
            query_params=query_params,
        )
        logger.debug("Javdb search actors query={} url={}", actor_name, url)
        payload = self.request_json("GET", url)
        actors = payload.get("data", {}).get("actors", [])
        logger.debug(
            "Javdb search actors candidates actor_name={} count={}", actor_name, len(actors))
        if not actors:
            logger.warning("Javdb actors not found actor_name={}", actor_name)
            raise MetadataNotFoundError("actor", actor_name)

        resources: list[JavdbMovieActorResource] = []
        seen_actor_ids: set[str] = set()
        for actor in actors:
            actor_id = actor.get("id")
            if not actor_id or actor_id in seen_actor_ids:
                continue
            seen_actor_ids.add(actor_id)
            raw_gender = actor.get("gender")
            gender = 0 if raw_gender is None else int(not raw_gender)
            resources.append(
                JavdbMovieActorResource.model_validate(
                    {
                        "javdb_id": actor_id,
                        "javdb_type": actor.get("type") or 0,
                        "name": actor.get("name") or "",
                        "alias_names": self._collect_actor_candidate_names(actor),
                        "avatar_url": self._resolve_actor_avatar_url(actor),
                        "gender": gender,
                    }
                )
            )

        if not resources:
            logger.warning(
                "Javdb search actors has no valid candidates actor_name={}", actor_name)
            raise MetadataNotFoundError("actor", actor_name)
        logger.debug("Javdb search actors success actor_name={} count={}",
                     actor_name, len(resources))
        return resources

    def search_actor(self, actor_name: str) -> JavdbMovieActorResource:
        actor_info = self._search_actor(actor_name)
        return JavdbMovieActorResource.model_validate(
            {
                "javdb_id": actor_info["id"],
                "javdb_type": actor_info.get("type", 0),
                "name": actor_info.get("name", actor_name),
                "alias_names": actor_info.get("alias_names", []),
                "avatar_url": actor_info.get("avatar_url"),
                "gender": actor_info.get("gender", 0),
            }
        )

    def _search_movie(self, movie_number: str) -> dict[str, Any]:
        normalized_number = normalize_movie_number(movie_number)
        query_params = {
            "q": normalized_number,
            **self.API_PARAMS_MOVIE_SEARCH,
        }
        url = self._build_api_url(
            path=self.API_PATH_SEARCH,
            query_params=query_params,
        )
        logger.debug(
            "Javdb search movie start movie_number={} normalized={}",
            movie_number,
            normalized_number,
        )
        payload = self.request_json("GET", url)
        movies = payload.get("data", {}).get("movies", [])
        logger.debug("Javdb search movie candidates normalized={} count={}",
                     normalized_number, len(movies))
        for movie in sorted(movies, key=lambda m: m.get("release_date") or "", reverse=True):
            if normalize_movie_number(movie.get("number", "")) == normalized_number:
                logger.debug(
                    "Javdb search movie matched movie_number={} javdb_id={}",
                    movie_number,
                    movie.get("id"),
                )
                return movie
        logger.warning("Javdb movie not found movie_number={}", movie_number)
        raise MetadataNotFoundError("movie", movie_number)

    def get_movie_by_javdb_id(self, javdb_id: str) -> JavdbMovieDetailResource:
        logger.debug("Javdb get_movie_by_javdb_id start javdb_id={}", javdb_id)
        payload = self._get_movie_detail_payload(javdb_id)
        detail = self._build_movie_detail(payload)
        logger.debug(
            "Javdb get_movie_by_javdb_id success javdb_id={} movie_number={} actors={} tags={} plot_images={}",
            javdb_id,
            detail.movie_number,
            len(detail.actors),
            len(detail.tags),
            len(detail.plot_images),
        )
        return detail

    def get_movie_by_number(self, movie_number: str) -> JavdbMovieDetailResource:
        logger.debug(
            "Javdb get_movie_by_number start movie_number={}", movie_number)
        movie = self._search_movie(movie_number)
        movie_id = movie.get("id")
        if not movie_id:
            raise MetadataNotFoundError("movie", movie_number)
        logger.debug(
            "Javdb get_movie_by_number resolved movie_number={} javdb_id={}", movie_number, movie_id)
        return self.get_movie_by_javdb_id(movie_id)

    def get_movie_detail(self, movie_number: str) -> JavdbMovieDetailResource:
        return self.get_movie_by_number(movie_number)

    def get_movie_reviews_by_javdb_id(
        self,
        javdb_id: str,
        page: int = 1,
        limit: int = 20,
        sort_by: str = "recently",
    ) -> list[JavdbMovieReviewResource]:
        logger.debug(
            "Javdb get_movie_reviews_by_javdb_id start javdb_id={} page={} limit={} sort_by={}",
            javdb_id,
            page,
            limit,
            sort_by,
        )
        payload, url = self._get_movie_reviews_payload(
            javdb_id=javdb_id,
            page=page,
            limit=limit,
            sort_by=sort_by,
        )
        reviews = self._extract_movie_reviews(payload, url=url)
        resources: list[JavdbMovieReviewResource] = []
        for review in reviews:
            if not isinstance(review, dict):
                logger.warning(
                    "Javdb movie review entry skipped because type is invalid javdb_id={} value_type={}",
                    javdb_id,
                    type(review).__name__,
                )
                continue
            resources.append(self._build_movie_review(review))
        logger.debug(
            "Javdb get_movie_reviews_by_javdb_id success javdb_id={} page={} reviews={}",
            javdb_id,
            page,
            len(resources),
        )
        return resources

    def get_hot_reviews(
        self,
        period: str = "weekly",
        page: int = 1,
        limit: int = 24,
    ) -> list[JavdbMovieReviewResource]:
        if period not in self.SUPPORTED_HOT_REVIEW_PERIODS:
            raise ValueError(f"unsupported period: {period}")
        if page < 1:
            raise ValueError(f"invalid page: {page}")
        if limit < 1:
            raise ValueError(f"invalid limit: {limit}")

        logger.debug(
            "Javdb get_hot_reviews start period={} page={} limit={}",
            period,
            page,
            limit,
        )
        payload, url = self._get_hot_reviews_payload(
            period=period,
            page=page,
            limit=limit,
        )
        reviews = self._extract_movie_reviews(payload, url=url)
        resources: list[JavdbMovieReviewResource] = []
        for review in reviews:
            if not isinstance(review, dict):
                logger.warning(
                    "Javdb hot review entry skipped because type is invalid value_type={}",
                    type(review).__name__,
                )
                continue
            resources.append(self._build_movie_review(review))
        logger.debug(
            "Javdb get_hot_reviews success period={} page={} reviews={}",
            period,
            page,
            len(resources),
        )
        return resources

    def get_rank_numbers(self, video_type: str, period: str = "daily") -> list[str]:
        if video_type not in self.SUPPORTED_RANK_VIDEO_TYPES:
            raise ValueError(f"unsupported video_type: {video_type}")
        if period not in self.SUPPORTED_RANK_PERIODS:
            raise ValueError(f"unsupported period: {period}")

        movies = self._fetch_rank_movies(video_type=video_type, period=period)
        numbers: list[str] = []
        for movie in movies:
            number = movie.get("number")
            if number:
                numbers.append(number)
        logger.debug(
            "Javdb get_rank_numbers success video_type={} period={} count={}",
            video_type,
            period,
            len(numbers),
        )
        return numbers

    def _device_payload(self) -> dict[str, str]:
        # device_uuid 按账号确定性派生：同账号每次登录一致，不同账号互不相同。
        device_uuid = str(uuid.uuid5(self.DEVICE_UUID_NAMESPACE, self.username or ""))
        return {"device_uuid": device_uuid, **self.DEVICE}

    def _ensure_logged_in(self) -> str:
        # 懒登录：已有 token 直接复用；本实例曾登录失败则不再重试，避免触发风控/锁号。
        if self._token:
            return self._token
        if self._login_failed:
            raise JavdbAuthError("login already failed for this provider instance")
        if not (self.username and self.password):
            self._login_failed = True
            raise JavdbAuthError("javdb account is not configured")

        url = self._build_api_url(path=self.API_PATH_SESSIONS)
        payload = {
            "username": self.username,
            "password": self.password,
            **self._device_payload(),
        }
        # 登录额外头：服务端虽声明 multipart，但接受 form-urlencoded 提交。
        headers = {
            "user-agent": "Dart/3.5 (dart:io)",
            "Content-Type": "multipart/form-data",
        }
        try:
            body = self.request_json("POST", url, data=payload, headers=headers)
        except MetadataRequestError as exc:
            self._login_failed = True
            raise JavdbAuthError(str(exc)) from exc

        token = (body.get("data") or {}).get("token")
        if not token:
            self._login_failed = True
            message = body.get("message") or "login response missing token"
            logger.warning("Javdb login failed detail={}", message)
            raise JavdbAuthError(message)

        self._token = token
        logger.info("Javdb login success token_len={}", len(token))
        return token

    def get_top_numbers(
        self,
        top_type: str,
        type_value: str = "",
        max_pages: int | None = None,
    ) -> list[str]:
        # 需登录的 TOP250 榜：type=all/year/video_type，分页抓取最多 max_pages 页。
        if top_type not in self.SUPPORTED_TOP_TYPES:
            raise ValueError(f"unsupported top_type: {top_type}")
        page_count = self.TOP250_MAX_PAGES if max_pages is None else max_pages

        token = self._ensure_logged_in()
        numbers: list[str] = []
        for page in range(1, page_count + 1):
            url = self._build_api_url(
                path=self.API_PATH_MOVIES_TOP,
                query_params={
                    "start_rank": 1,
                    "type": top_type,
                    "type_value": type_value,
                    "ignore_watched": "false",
                    "page": page,
                    "limit": self.TOP250_PAGE_LIMIT,
                },
            )
            payload = self.request_json(
                "GET", url, headers={"authorization": f"Bearer {token}"})
            if payload.get("success") != 1:
                detail = payload.get(
                    "message") or f"unexpected success={payload.get('success')}"
                logger.warning(
                    "Javdb top request returned unsuccessful payload top_type={} type_value={} page={} detail={}",
                    top_type,
                    type_value,
                    page,
                    detail,
                )
                raise MetadataRequestError("GET", url, detail)

            data = payload.get("data")
            movies = data.get("movies") if isinstance(data, dict) else None
            if not isinstance(movies, list):
                detail = "missing data.movies"
                logger.warning(
                    "Javdb top payload missing movies top_type={} type_value={} page={}",
                    top_type,
                    type_value,
                    page,
                )
                raise MetadataRequestError("GET", url, detail)
            if not movies:
                # 空页即到底，停止翻页。
                break
            for movie in movies:
                number = movie.get("number")
                if number:
                    numbers.append(number)
        logger.debug(
            "Javdb get_top_numbers success top_type={} type_value={} count={}",
            top_type,
            type_value,
            len(numbers),
        )
        return numbers

    def get_playback_rank_numbers(self, filter_by: str = "all", period: str = "daily") -> list[str]:
        # 播放榜：filter_by=all(热播)/high_score(高评分)。
        if filter_by not in self.SUPPORTED_PLAYBACK_FILTERS:
            raise ValueError(f"unsupported filter_by: {filter_by}")
        if period not in self.SUPPORTED_RANK_PERIODS:
            raise ValueError(f"unsupported period: {period}")

        url = self._build_api_url(
            path=self.API_PATH_RANKINGS_PLAYBACK,
            query_params={"filter_by": filter_by, "period": period},
        )
        logger.debug(
            "Javdb fetch playback rank filter_by={} period={} url={}", filter_by, period, url)
        payload = self.request_json("GET", url)
        # playback 响应与 /api/v1/rankings 同构，带 success 包络，先校验再取 movies。
        if payload.get("success") != 1:
            detail = payload.get(
                "message") or f"unexpected success={payload.get('success')}"
            logger.warning(
                "Javdb playback rank request returned unsuccessful payload filter_by={} period={} detail={}",
                filter_by,
                period,
                detail,
            )
            raise MetadataRequestError("GET", url, detail)

        data = payload.get("data")
        movies = data.get("movies") if isinstance(data, dict) else None
        if not isinstance(movies, list):
            detail = "missing data.movies"
            logger.warning(
                "Javdb playback rank payload missing movies filter_by={} period={}",
                filter_by,
                period,
            )
            raise MetadataRequestError("GET", url, detail)

        numbers: list[str] = []
        for movie in movies:
            number = movie.get("number")
            if number:
                numbers.append(number)
        logger.debug(
            "Javdb get_playback_rank_numbers success filter_by={} period={} count={}",
            filter_by,
            period,
            len(numbers),
        )
        return numbers

    def build_request_headers(self) -> dict[str, str]:
        return {
            "connection": "keep-alive",
            "accept-language": "zh-TW",
            "host": self.host,
            "jdsignature": self._get_sign(),
        }

    def _get_sign(self) -> str:
        current_timestamp = int(time.time())
        secret = (
            f"{current_timestamp}"
            "71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa"
        )
        sign = hashlib.md5(secret.encode()).hexdigest()
        return f"{current_timestamp}.lpw6vgqzsp.{sign}"

    def _build_movie_list_item(self, movie: dict[str, Any]) -> JavdbMovieListItemResource:
        movie_id = movie["id"]
        release_date = _parse_date(movie.get("release_date"))
        cover_url = movie.get("cover_url")
        return JavdbMovieListItemResource.model_validate(
            {
                "javdb_id": movie_id,
                "movie_number": movie["number"],
                "title": movie.get("title", ""),
                "release_date": release_date,
                "cover_image": self._normalize_image_url(cover_url),
                "duration_minutes": movie.get("duration") or 0,
                "score": 0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_subscribed": None,
            }
        )

    def _get_movie_detail_payload(self, javdb_id: str) -> dict[str, Any]:
        url = self._build_api_url(
            path=self.API_PATH_MOVIE_DETAIL,
            path_params={"javdb_id": javdb_id},
            query_params=self.API_PARAMS_MOVIE_DETAIL,
        )
        logger.debug(
            "Javdb fetch movie detail javdb_id={} url={}", javdb_id, url)
        payload = self.request_json("GET", url)
        if payload.get("success") != 1:
            detail = payload.get(
                "message") or f"unexpected success={payload.get('success')}"
            logger.warning(
                "Javdb detail request returned unsuccessful payload javdb_id={} detail={}", javdb_id, detail)
            raise MetadataRequestError("GET", url, detail)
        movie = payload.get("data", {}).get("movie")
        logger.debug(json.dumps(payload, ensure_ascii=False))
        if not movie:
            logger.warning(
                "Javdb detail payload missing movie field javdb_id={}", javdb_id)
            raise MetadataNotFoundError("movie", javdb_id)
        logger.debug("Javdb detail payload received javdb_id={} keys={}",
                     javdb_id, list(movie.keys()))
        return payload

    def _get_movie_reviews_payload(
        self,
        *,
        javdb_id: str,
        page: int,
        limit: int,
        sort_by: str | None,
    ) -> tuple[dict[str, Any], str]:
        params: dict[str, Any] = {
            **self.API_PARAMS_MOVIE_REVIEWS,
        }
        params["page"] = page
        params["limit"] = limit
        if sort_by:
            params["sort_by"] = sort_by
        url = self._build_api_url(
            path=self.API_PATH_MOVIE_REVIEWS,
            path_params={"javdb_id": javdb_id},
            query_params=params,
        )
        logger.debug(
            "Javdb fetch movie reviews javdb_id={} page={} limit={} sort_by={} url={}",
            javdb_id,
            page,
            limit,
            sort_by,
            url,
        )
        payload = self.request_json("GET", url)
        if payload.get("success") != 1:
            detail = payload.get(
                "message") or f"unexpected success={payload.get('success')}"
            logger.warning(
                "Javdb reviews request returned unsuccessful payload javdb_id={} detail={}",
                javdb_id,
                detail,
            )
            raise MetadataRequestError("GET", url, detail)
        return payload, url

    def _get_hot_reviews_payload(
        self,
        *,
        period: str,
        page: int,
        limit: int,
    ) -> tuple[dict[str, Any], str]:
        params: dict[str, Any] = {
            **self.API_PARAMS_HOT_REVIEWS,
        }
        params["period"] = period
        params["page"] = page
        params["limit"] = limit
        url = self._build_api_url(
            path=self.API_PATH_HOT_REVIEWS,
            query_params=params,
        )
        logger.debug(
            "Javdb fetch hot reviews period={} page={} limit={} url={}",
            period,
            page,
            limit,
            url,
        )
        payload = self.request_json("GET", url)
        if payload.get("success") != 1:
            detail = payload.get(
                "message") or f"unexpected success={payload.get('success')}"
            logger.warning(
                "Javdb hot reviews request returned unsuccessful payload period={} detail={}",
                period,
                detail,
            )
            raise MetadataRequestError("GET", url, detail)
        return payload, url

    def _extract_movie_reviews(self, payload: dict[str, Any], *, url: str) -> list[Any]:
        data = payload.get("data")
        reviews = data.get("reviews") if isinstance(data, dict) else None
        if isinstance(reviews, list):
            return reviews
        logger.warning("Javdb reviews payload missing reviews list url={}", url)
        raise MetadataRequestError("GET", url, "missing data.reviews")

    def _safe_float(self, value: Any, default: float | None = None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_review_movie(self, movie: Any) -> JavdbReviewMovieResource | None:
        if not isinstance(movie, dict):
            return None
        mapped_payload = {
            "id": str(movie.get("id") or ""),
            "number": movie.get("number") or "",
            "title": movie.get("title") or "",
            "origin_title": movie.get("origin_title"),
            "score": self._safe_float(movie.get("score")),
            "thumb_url": movie.get("thumb_url"),
            "release_date": movie.get("release_date"),
        }
        return JavdbReviewMovieResource.model_validate(mapped_payload)

    def _build_movie_review(self, review: dict[str, Any]) -> JavdbMovieReviewResource:
        mapped_payload = {
            "id": safe_int(review.get("id"), 0),
            "score": safe_int(review.get("score"), 0),
            "content": review.get("content") or "",
            "created_at": review.get("created_at"),
            "username": review.get("username") or "",
            "like_count": safe_int(review.get("likes_count"), 0),
            "watch_count": safe_int(review.get("watched_count"), 0),
            "movie": self._build_review_movie(review.get("movie")),
        }
        return JavdbMovieReviewResource.model_validate(mapped_payload)

    def _fetch_rank_movies(self, video_type: str, period: str) -> list[dict[str, Any]]:
        url = self._build_api_url(
            path=self.API_PATH_RANKINGS,
            query_params={"type": video_type, "period": period},
        )
        logger.debug(
            "Javdb fetch rank movies video_type={} period={} url={}", video_type, period, url)
        payload = self.request_json("GET", url)
        if payload.get("success") != 1:
            detail = payload.get(
                "message") or f"unexpected success={payload.get('success')}"
            logger.warning(
                "Javdb rank request returned unsuccessful payload video_type={} period={} detail={}",
                video_type,
                period,
                detail,
            )
            raise MetadataRequestError("GET", url, detail)

        data = payload.get("data")
        movies = data.get("movies") if isinstance(data, dict) else None
        if not isinstance(movies, list):
            detail = "missing data.movies"
            logger.warning(
                "Javdb rank payload missing movies video_type={} period={}",
                video_type,
                period,
            )
            raise MetadataRequestError("GET", url, detail)
        return movies

    def _normalize_movie_list_field(
        self,
        value: Any,
        *,
        field_name: str,
        javdb_id: str,
    ) -> list[Any]:
        if value is None:
            logger.debug(
                "Javdb movie detail field is null javdb_id={} field={}",
                javdb_id,
                field_name,
            )
            return []
        if isinstance(value, list):
            return value
        logger.warning(
            "Javdb movie detail field is not list javdb_id={} field={} value_type={}",
            javdb_id,
            field_name,
            type(value).__name__,
        )
        return []

    def _build_movie_detail(self, payload: dict[str, Any]) -> JavdbMovieDetailResource:
        movie = payload.get("data", {}).get("movie", {})
        movie_id = movie["id"]
        actors = self._normalize_movie_list_field(
            movie.get("actors"),
            field_name="actors",
            javdb_id=movie_id,
        )
        tags = self._normalize_movie_list_field(
            movie.get("tags"),
            field_name="tags",
            javdb_id=movie_id,
        )
        preview_images = self._normalize_movie_list_field(
            movie.get("preview_images"),
            field_name="preview_images",
            javdb_id=movie_id,
        )
        release_date = _parse_date(movie.get("release_date"))
        detail = JavdbMovieDetailResource.model_validate(
            {
                "javdb_id": movie_id,
                "movie_number": movie["number"],
                "title": movie.get("title") or "",
                "summary": movie.get("summary") or "",
                "cover_image": self._normalize_image_url(movie.get("cover_url")),
                "release_date": release_date,
                "duration_minutes": movie.get("duration") or 0,
                "score": movie.get("score"),
                "watched_count": movie.get("watched_count") or 0,
                "want_watch_count": movie.get("want_watch_count") or 0,
                "comment_count": movie.get("comments_count") or 0,
                "score_number": movie.get("reviews_count") or 0,
                "is_subscribed": None,
                "series_name": movie.get("series_name"),
                "maker_name": movie.get("maker_name"),
                "director_name": movie.get("director_name"),
                "thin_cover_image": None,
                "extra": payload,
                "actors": self._build_movie_actors(actors),
                "tags": self._build_movie_tags(tags),
                "plot_images": self._build_preview_images(preview_images),
            }
        )
        logger.debug(
            "Javdb movie detail mapped javdb_id={} movie_number={} actors={} tags={} plot_images={}",
            detail.javdb_id,
            detail.movie_number,
            len(detail.actors),
            len(detail.tags),
            len(detail.plot_images),
        )
        return detail

    def _build_movie_actors(self, actors: list[Any]) -> list[JavdbMovieActorResource]:
        resources: list[JavdbMovieActorResource] = []
        for actor in actors:
            if not isinstance(actor, dict):
                logger.warning(
                    "Javdb actor entry skipped because type is invalid value_type={}",
                    type(actor).__name__,
                )
                continue
            actor_id = actor.get("id")
            if not actor_id:
                logger.debug("Javdb actor entry skipped because id is missing")
                continue
            resources.append(
                JavdbMovieActorResource.model_validate(
                    {
                        "javdb_id": actor_id,
                        "javdb_type": actor.get("type") or 0,
                        "name": actor.get("name") or "",
                        "alias_names": self._collect_actor_candidate_names(actor),
                        "avatar_url": self._resolve_actor_avatar_url(actor),
                        "gender": int(not actor.get("gender")),
                    }
                )
            )
        logger.debug("Javdb actor resources built count={}", len(resources))
        return resources

    def _collect_actor_candidate_names(self, actor: dict[str, Any]) -> list[str]:
        candidate_names: list[str] = []
        seen_names: set[str] = set()
        primary_names = [
            actor.get("name") or "",
            actor.get("name_zht") or "",
        ]
        for candidate_name in primary_names:
            candidate_name = candidate_name.strip()
            if not candidate_name:
                continue
            normalized_name = candidate_name.casefold()
            if normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            candidate_names.append(candidate_name)

        other_name = actor.get("other_name") or ""
        for candidate_name in other_name.split(","):
            candidate_name = candidate_name.strip()
            if not candidate_name:
                continue
            normalized_name = candidate_name.casefold()
            if normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            candidate_names.append(candidate_name)
        return candidate_names

    def _resolve_actor_avatar_url(self, actor: dict[str, Any]) -> str | None:
        return self._normalize_image_url(actor.get("avatar_url"))

    def _build_movie_tags(self, tags: list[Any]) -> list[JavdbMovieTagResource]:
        resources: list[JavdbMovieTagResource] = []
        for tag in tags:
            if not isinstance(tag, dict):
                logger.warning(
                    "Javdb tag entry skipped because type is invalid value_type={}",
                    type(tag).__name__,
                )
                continue
            resources.append(
                JavdbMovieTagResource.model_validate(
                    {
                        "javdb_id": str(tag.get("id", "")),
                        "name": tag.get("name", ""),
                    }
                )
            )
        logger.debug("Javdb tag resources built count={}", len(resources))
        return resources

    def _build_preview_images(
        self, preview_images: list[Any]
    ) -> list[str]:
        resources: list[str] = []
        for image in preview_images:
            if not isinstance(image, dict):
                logger.warning(
                    "Javdb preview image entry skipped because type is invalid value_type={}",
                    type(image).__name__,
                )
                continue
            normalized_url = self._normalize_image_url(
                image.get("large_url", ""))
            if normalized_url:
                resources.append(normalized_url)
            else:
                logger.debug(
                    "Javdb preview image skipped because url cannot normalize raw={}", image.get("large_url"))
        logger.debug("Javdb preview images built count={}", len(resources))
        return resources
