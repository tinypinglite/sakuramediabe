import hashlib
import json
import time
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from loguru import logger

from src.common import normalize_movie_number
from src.metadata.gfriends import GfriendsActorImageResolver
from src.metadata.provider import (
    MetadataNotFoundError,
    MetadataRequestClient,
    MetadataRequestError,
)
from src.schema.metadata.javdb import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
    JavdbMovieTagResource,
)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


class JavdbProvider(MetadataRequestClient):
    def __init__(
        self,
        host: str,
        proxy: Optional[str] = None,
        actor_image_resolver: Optional[GfriendsActorImageResolver] = None,
    ):
        MetadataRequestClient.__init__(self, proxy=proxy)
        self.host = host
        self.actor_image_resolver = actor_image_resolver
        logger.info("JavdbProvider initialized host={} proxy_enabled={}", host, bool(proxy))

    def _normalize_image_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if "covers" in url:
            return f"https://c0.jdbstatic.com/covers/{url.split('covers/')[-1]}"
        if "samples" in url:
            return f"https://c0.jdbstatic.com/samples/{url.split('samples/')[-1]}"
        if "avatars" in url:
            return f"https://c0.jdbstatic.com/avatars/{url.split('avatars/')[-1]}"
        return url
        
    
    def get_actor_movies(
        self, actor_name: str, page: int = 1
    ) -> list[JavdbMovieListItemResource]:
        logger.debug("Javdb get_actor_movies start actor_name={} page={}", actor_name, page)
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
    ) -> List[JavdbMovieListItemResource]:
        # logger.info(f"Searching actor {actor_name} {actor_id}")
        # url = (
            # f"https://{self.host}/api/v1/actors/{actor_id}/movies"
            # f"?page={page}&limit={page_size}&sort_by=release"
        # )
        logger.debug(
            "Javdb get_actor_movies_by_javdb start actor_javdb_id={} actor_type={} page={}",
            actor_javdb_id,
            actor_type,
            page,
        )
        url = (
            f"https://{self.host}/api/v1/movies/tags"
            f"?filter_by={actor_type}:a:{actor_javdb_id}&sort_by=release&order_by=desc&page={page}"
        )
        # url = f"https://{self.host}/api/v1/actors/{actor_info['id']}"
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

    def _search_actor(self, actor_name: str) -> dict:
        query = quote(actor_name, safe="-")
        url = (
            f"https://{self.host}/api/v2/search"
            f"?q={query}&from_recent=false&type=actor&page=1&limit=24"
        )
        logger.debug("Javdb search actor query={} url={}", actor_name, url)
        payload = self.request_json("GET", url)
        actors = payload.get("data", {}).get("actors", [])
        logger.debug("Javdb search actor candidates actor_name={} count={}", actor_name, len(actors))
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
                "avatar_url": self._resolve_actor_avatar_url(target_actor),
                "gender": gender,
            }
        logger.warning("Javdb actor not matched in candidate list actor_name={}", actor_name)
        raise MetadataNotFoundError("actor", actor_name)

    def search_actors(self, actor_name: str) -> List[JavdbMovieActorResource]:
        query = quote(actor_name, safe="-")
        url = (
            f"https://{self.host}/api/v2/search"
            f"?q={query}&from_recent=false&type=actor&page=1&limit=24"
        )
        logger.debug("Javdb search actors query={} url={}", actor_name, url)
        payload = self.request_json("GET", url)
        actors = payload.get("data", {}).get("actors", [])
        logger.debug("Javdb search actors candidates actor_name={} count={}", actor_name, len(actors))
        if not actors:
            logger.warning("Javdb actors not found actor_name={}", actor_name)
            raise MetadataNotFoundError("actor", actor_name)

        resources: List[JavdbMovieActorResource] = []
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
                        "avatar_url": self._resolve_actor_avatar_url(actor),
                        "gender": gender,
                    }
                )
            )

        if not resources:
            logger.warning("Javdb search actors has no valid candidates actor_name={}", actor_name)
            raise MetadataNotFoundError("actor", actor_name)
        logger.debug("Javdb search actors success actor_name={} count={}", actor_name, len(resources))
        return resources

    def search_actor(self, actor_name: str) -> JavdbMovieActorResource:
        actor_info = self._search_actor(actor_name)
        return JavdbMovieActorResource.model_validate(
            {
                "javdb_id": actor_info["id"],
                "javdb_type": actor_info.get("type", 0),
                "name": actor_info.get("name", actor_name),
                "avatar_url": actor_info.get("avatar_url"),
                "gender": actor_info.get("gender", 0),
            }
        )

    def _search_movie(self, movie_number: str) -> Dict[str, Any]:
        normalized_number = normalize_movie_number(movie_number)
        query = quote(normalized_number, safe="-")
        url = (
            f"https://{self.host}/api/v2/search"
            f"?q={query}&from_recent=false&type=movie&movie_type=all"
            f"&movie_sort_by=relevance&movie_filter_by=all&page=1&limit=24"
        )
        logger.debug(
            "Javdb search movie start movie_number={} normalized={}",
            movie_number,
            normalized_number,
        )
        payload = self.request_json("GET", url)
        movies = payload.get("data", {}).get("movies", [])
        logger.debug("Javdb search movie candidates normalized={} count={}", normalized_number, len(movies))
        for movie in movies:
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
        logger.debug("Javdb get_movie_by_number start movie_number={}", movie_number)
        movie = self._search_movie(movie_number)
        movie_id = movie.get("id")
        if not movie_id:
            raise MetadataNotFoundError("movie", movie_number)
        logger.debug("Javdb get_movie_by_number resolved movie_number={} javdb_id={}", movie_number, movie_id)
        return self.get_movie_by_javdb_id(movie_id)

    def get_movie_detail(self, movie_number: str) -> JavdbMovieDetailResource:
        return self.get_movie_by_number(movie_number)

  
    def build_request_headers(self) -> Dict[str, str]:
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

    def _build_movie_list_item(self, movie: Dict[str, Any]) -> JavdbMovieListItemResource:
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

    def _get_movie_detail_payload(self, javdb_id: str) -> Dict[str, Any]:
        url = f"https://{self.host}/api/v4/movies/{javdb_id}?from_rankings=true"
        logger.debug("Javdb fetch movie detail javdb_id={} url={}", javdb_id, url)
        payload = self.request_json("GET", url)
        if payload.get("success") != 1:
            detail = payload.get("message") or f"unexpected success={payload.get('success')}"
            logger.warning("Javdb detail request returned unsuccessful payload javdb_id={} detail={}", javdb_id, detail)
            raise MetadataRequestError("GET", url, detail)
        movie = payload.get("data", {}).get("movie")
        logger.debug(json.dumps(payload, ensure_ascii=False))
        if not movie:
            logger.warning("Javdb detail payload missing movie field javdb_id={}", javdb_id)
            raise MetadataNotFoundError("movie", javdb_id)
        logger.debug("Javdb detail payload received javdb_id={} keys={}", javdb_id, list(movie.keys()))
        return payload

    def _build_movie_detail(self, payload: Dict[str, Any]) -> JavdbMovieDetailResource:
        movie = payload.get("data", {}).get("movie", {})
        release_date = _parse_date(movie.get("release_date"))
        detail = JavdbMovieDetailResource.model_validate(
            {
                "javdb_id": movie["id"],
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
                "thin_cover_image": None,
                "extra": payload,
                "actors": self._build_movie_actors(movie.get("actors", [])),
                "tags": self._build_movie_tags(movie.get("tags", [])),
                "plot_images": self._build_preview_images(movie.get("preview_images", [])),
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

    def _build_movie_actors(self, actors: List[Dict[str, Any]]) -> List[JavdbMovieActorResource]:
        resources: List[JavdbMovieActorResource] = []
        for actor in actors:
            resources.append(
                JavdbMovieActorResource.model_validate(
                    {
                        "javdb_id": actor['id'],
                        "name": actor.get("name") or "",
                        "avatar_url": self._resolve_actor_avatar_url(actor),
                        "gender":  int(not actor.get('gender'))
                    }
                )
            )
        logger.debug("Javdb actor resources built count={}", len(resources))
        return resources

    def _collect_actor_candidate_names(self, actor: Dict[str, Any]) -> List[str]:
        candidate_names: List[str] = []
        primary_names = [
            actor.get("name") or "",
            actor.get("name_zht") or "",
        ]
        for candidate_name in primary_names:
            candidate_name = candidate_name.strip()
            if candidate_name:
                candidate_names.append(candidate_name)

        other_name = actor.get("other_name") or ""
        for candidate_name in other_name.split(","):
            candidate_name = candidate_name.strip()
            if candidate_name:
                candidate_names.append(candidate_name)
        return candidate_names

    def _resolve_actor_avatar_url(self, actor: Dict[str, Any]) -> Optional[str]:
        fallback_url = self._normalize_image_url(actor.get("avatar_url"))
        if self.actor_image_resolver is None:
            return fallback_url

        candidate_names = self._collect_actor_candidate_names(actor)
        if not candidate_names:
            return fallback_url

        try:
            resolved_url = self.actor_image_resolver.resolve(candidate_names)
            if resolved_url:
                return resolved_url
        except Exception as exc:
            logger.warning(
                "Gfriends actor image resolve failed actor_id={} actor_name={} detail={}",
                actor.get("id"),
                actor.get("name"),
                exc,
            )
        return fallback_url

    def _build_movie_tags(self, tags: List[Dict[str, Any]]) -> List[JavdbMovieTagResource]:
        resources: List[JavdbMovieTagResource] = []
        for tag in tags:
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
        self, preview_images: List[Dict[str, Any]]
    ) -> List[str]:
        resources: List[str] = []
        for image in preview_images:
            normalized_url = self._normalize_image_url(image.get("large_url", ""))
            if normalized_url:
                resources.append(normalized_url)
            else:
                logger.debug("Javdb preview image skipped because url cannot normalize raw={}", image.get("large_url"))
        logger.debug("Javdb preview images built count={}", len(resources))
        return resources
