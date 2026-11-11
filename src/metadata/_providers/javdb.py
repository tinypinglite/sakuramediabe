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

from .exceptions import (
    JavdbAuthError,
    JavdbAuthRequiredError,
    MetadataNotFoundError,
    MetadataRequestError,
)
from .http_client import MetadataRequestClient
from .javdb_auth import (
    JavdbAuthManager,
    get_javdb_auth_manager,
    javdb_account_fingerprint,
)
from .models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
    JavdbMovieReviewResource,
    JavdbMovieTagResource,
    JavdbReviewMovieResource,
)
from .utils import normalize_movie_number


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


class JavdbProvider(MetadataRequestClient):
    REQUEST_TIMEOUT_SECONDS = 60.0
    CONNECT_TIMEOUT_SECONDS = 10.0
    REQUEST_MAX_RETRIES = 1

    SUPPORTED_RANK_VIDEO_TYPES = {"0", "1", "3"}
    SUPPORTED_RANK_PERIODS = {"daily", "weekly", "monthly"}
    # 播放榜筛选：all=热播，high_score=高评分。
    SUPPORTED_PLAYBACK_FILTERS = {"all", "high_score"}
    API_PATH_MOVIES_TAGS = "/api/v1/movies/tags"
    API_PATH_SEARCH = "/api/v2/search"
    API_PATH_MOVIE_DETAIL = "/api/v4/movies/{javdb_id}"
    API_PATH_MOVIE_REVIEWS = "/api/v1/movies/{javdb_id}/reviews"
    API_PATH_RANKINGS = "/api/v1/rankings"
    API_PATH_RANKINGS_PLAYBACK = "/api/v1/rankings/playback"
    API_PATH_SESSIONS = "/api/v1/sessions"
    API_PATH_MOVIES_TOP = "/api/v1/movies/top"

    # TOP250：每页 50 条，5 页满 250。
    TOP250_PAGE_LIMIT = 50
    TOP250_MAX_PAGES = 5
    SUPPORTED_TOP_TYPES = {"all", "year", "video_type"}

    # 登录所需的通用 Android 设备指纹，不包含具体厂商和机型特征。
    DEVICE = {
        "device_name": "Android",
        "device_model": "Android",
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
    API_PARAMS_ACTOR_SEARCH = {
        "from_recent": "false",
        "type": "actor",
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

    def __init__(
        self,
        host: str,
        *,
        username: str | None = None,
        password: str | None = None,
        auth_manager: JavdbAuthManager | None = None,
    ):
        MetadataRequestClient.__init__(
            self,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            connect_timeout=self.CONNECT_TIMEOUT_SECONDS,
            max_retries=self.REQUEST_MAX_RETRIES,
        )
        self.host = host
        self.username = username
        self.password = password
        if auth_manager is None:
            from src.config.config import settings

            auth_manager = get_javdb_auth_manager(
                settings.metadata.javdb_session_path
            )
        self._auth_manager = auth_manager
        self._account_fingerprint = javdb_account_fingerprint(
            host, username, password
        )
        logger.info(
            "JavdbProvider initialized host={} account_configured={}",
            host,
            bool(username and password),
        )

    @property
    def account_configured(self) -> bool:
        return bool(self.username and self.password)

    @staticmethod
    def _normalize_image_url(url: str | None) -> str | None:
        """原样保留 app CDN 图片地址。

        v4/v2 API 的 ``cover_url`` / ``samples`` / ``avatars`` 都指向 app CDN
        （``tp.spfcas.com/<tenant>/...``），内容是干净原图，只是字节被单字节 XOR
        混淆，由下载层 ``deobfuscate_image_bytes`` 统一还原。

        历史实现把 host 改写成网页 CDN ``c0.jdbstatic.com`` 以绕开混淆，代价是
        ``covers/`` 会拿到带 ``javdb.com`` 水印的版本（同一张图的另一份内容），
        因此这里不再改写，换源等于换图。
        """
        return url or None

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

    @staticmethod
    def _is_auth_required_payload(payload: dict[str, Any]) -> bool:
        if payload.get("success") == 1:
            return False
        message = str(payload.get("message") or "")
        action = str(payload.get("action") or "")
        return action == "JWTVerificationError" or any(
            marker in message for marker in ("請登錄", "请登录", "請先登錄", "请先登录")
        )

    def _raise_auth_required(self, payload: dict[str, Any] | None = None) -> None:
        detail = "javdb login required"
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("action") or detail)
        raise JavdbAuthRequiredError(
            detail,
            account_configured=self.account_configured,
        )

    def _request_api_json(
        self,
        method: str,
        *,
        path: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: str = "on_demand",
    ) -> dict[str, Any]:
        """请求 JavDB API 并统一处理成功包络与登录态。

        auth:
        - required: 请求前先登录；
        - on_demand: 先匿名请求，遇到认证错误再登录重试；
        - none: 永不登录。
        """

        url = self._build_api_url(
            path=path,
            path_params=path_params,
            query_params=query_params,
        )
        token = self._ensure_logged_in() if auth == "required" else None
        for attempt in range(2):
            request_headers = dict(headers or {})
            if token:
                request_headers["authorization"] = f"Bearer {token}"
            try:
                payload = self.request_json(
                    method,
                    url,
                    data=data,
                    headers=request_headers or None,
                )
            except MetadataRequestError as exc:
                if exc.status_code not in (401, 403):
                    raise
                if auth == "none" or not self.account_configured:
                    self._raise_auth_required()
                if attempt == 0:
                    self._auth_manager.invalidate(
                        token,
                        account_fingerprint=self._account_fingerprint,
                    )
                    token = self._ensure_logged_in()
                    continue
                self._raise_auth_required()

            if not isinstance(payload, dict):
                raise MetadataRequestError(
                    method, url, "response is not a JSON object"
                )
            if self._is_auth_required_payload(payload):
                if auth == "none" or not self.account_configured:
                    self._raise_auth_required(payload)
                if attempt == 0:
                    self._auth_manager.invalidate(
                        token,
                        account_fingerprint=self._account_fingerprint,
                    )
                    token = self._ensure_logged_in()
                    continue
                self._raise_auth_required(payload)

            if payload.get("success") != 1:
                detail = payload.get("message") or (
                    f"unexpected success={payload.get('success')}"
                )
                raise MetadataRequestError(method, url, str(detail))
            return payload

        self._raise_auth_required()
        raise AssertionError("unreachable")

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
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_MOVIES_TAGS,
            query_params=query_params,
            auth="on_demand",
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MetadataRequestError(
                "GET",
                self._build_api_url(path=self.API_PATH_MOVIES_TAGS),
                "missing data",
            )
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

    def _search_actor(self, actor_name: str) -> dict:
        query_params = {
            "q": actor_name,
            **self.API_PARAMS_ACTOR_SEARCH,
        }
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_SEARCH,
            query_params=query_params,
            auth="on_demand",
        )
        data = payload.get("data")
        actors = data.get("actors") if isinstance(data, dict) else None
        if not isinstance(actors, list):
            actors = []
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
            gender = self._map_actor_gender(target_actor.get("gender"))
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
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_SEARCH,
            query_params=query_params,
            auth="on_demand",
        )
        data = payload.get("data")
        actors = data.get("actors") if isinstance(data, dict) else None
        if not isinstance(actors, list):
            actors = []
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
            gender = self._map_actor_gender(actor.get("gender"))
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
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_SEARCH,
            query_params=query_params,
            auth="required",
        )
        data = payload.get("data")
        movies = data.get("movies") if isinstance(data, dict) else None
        if not isinstance(movies, list):
            movies = []
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

    def _login(self) -> str:
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
            if exc.status_code in (401, 403):
                raise JavdbAuthError(exc.detail) from exc
            raise

        token = (body.get("data") or {}).get("token")
        if not token:
            message = body.get("message") or "login response missing token"
            logger.warning("Javdb login failed detail={}", message)
            raise JavdbAuthError(message)

        logger.info("Javdb login success token_len={}", len(token))
        return token

    def _ensure_logged_in(self) -> str:
        if not self.account_configured:
            raise JavdbAuthRequiredError(
                "javdb account is not configured",
                account_configured=False,
            )
        return self._auth_manager.get_token(
            self._login,
            account_fingerprint=self._account_fingerprint,
        )

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

        numbers: list[str] = []
        for page in range(1, page_count + 1):
            query_params = {
                "start_rank": 1,
                "type": top_type,
                "type_value": type_value,
                "ignore_watched": "false",
                "page": page,
                "limit": self.TOP250_PAGE_LIMIT,
            }
            url = self._build_api_url(
                path=self.API_PATH_MOVIES_TOP,
                query_params=query_params,
            )
            payload = self._request_api_json(
                "GET",
                path=self.API_PATH_MOVIES_TOP,
                query_params=query_params,
                auth="required",
            )

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

        query_params = {"filter_by": filter_by, "period": period}
        url = self._build_api_url(
            path=self.API_PATH_RANKINGS_PLAYBACK,
            query_params=query_params,
        )
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_RANKINGS_PLAYBACK,
            query_params=query_params,
            auth="on_demand",
        )
        logger.debug(
            "Javdb fetch playback rank filter_by={} period={}", filter_by, period)

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
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_MOVIE_DETAIL,
            path_params={"javdb_id": javdb_id},
            query_params=self.API_PARAMS_MOVIE_DETAIL,
            auth="required",
        )
        data = payload.get("data")
        movie = data.get("movie") if isinstance(data, dict) else None
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
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_MOVIE_REVIEWS,
            path_params={"javdb_id": javdb_id},
            query_params=params,
            auth="on_demand",
        )
        logger.debug(
            "Javdb fetch movie reviews javdb_id={} page={} limit={} sort_by={}",
            javdb_id,
            page,
            limit,
            sort_by,
        )
        url = self._build_api_url(
            path=self.API_PATH_MOVIE_REVIEWS,
            path_params={"javdb_id": javdb_id},
            query_params=params,
        )
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
        query_params = {"type": video_type, "period": period}
        url = self._build_api_url(
            path=self.API_PATH_RANKINGS,
            query_params=query_params,
        )
        payload = self._request_api_json(
            "GET",
            path=self.API_PATH_RANKINGS,
            query_params=query_params,
            auth="on_demand",
        )
        logger.debug(
            "Javdb fetch rank movies video_type={} period={}", video_type, period)

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
                "actors_available": isinstance(movie.get("actors"), list),
                "tags_available": isinstance(movie.get("tags"), list),
                "plot_images": self._build_preview_images(preview_images),
            }
        )
        detail.actors_available = detail.actors_available and len(detail.actors) == len(actors)
        detail.tags_available = detail.tags_available and len(detail.tags) == len(tags)
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
                        "gender": self._map_actor_gender(actor.get("gender")),
                    }
                )
            )
        logger.debug("Javdb actor resources built count={}", len(resources))
        return resources

    @staticmethod
    def _map_actor_gender(raw_gender: Any) -> int:
        """将 JavDB 性别值映射为本地枚举：未知 0、女性 1、男性 2。"""
        if raw_gender is None:
            return 0
        if isinstance(raw_gender, str):
            normalized_gender = raw_gender.strip().lower()
            if normalized_gender in {"female", "女"}:
                return 1
            if normalized_gender in {"male", "男"}:
                return 2
            if normalized_gender not in {"0", "1"}:
                return 0
            raw_gender = int(normalized_gender)
        if raw_gender == 0:
            return 1
        if raw_gender == 1:
            return 2
        return 0

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
