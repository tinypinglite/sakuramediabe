import re
from typing import List, Optional

import httpx
import xmltodict
from loguru import logger

from src.config.config import settings
from src.common.movie_numbers import normalize_movie_number
from src.model import DownloadClient, Indexer
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.tag_rules import detect_candidate_tags


class JackettClientError(Exception):
    pass


class JackettClient:
    FC2_QUERY_PATTERN = re.compile(r"^FC2-?(\d+)$", re.IGNORECASE)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.indexer_settings.api_key
        self.client = client or httpx.Client(timeout=30.0, trust_env=False)

    def search(self, movie_number: str, indexer_kind: Optional[str] = None) -> List[DownloadCandidateResource]:
        candidates: List[DownloadCandidateResource] = []
        normalized_kind = (indexer_kind or "").strip().lower() or None
        search_query = self._build_search_query(movie_number)
        for indexer in (
            Indexer.select(Indexer, DownloadClient)
            .join(DownloadClient)
            .order_by(Indexer.id.asc())
        ):
            if normalized_kind and indexer.kind != normalized_kind:
                continue
            try:
                response = self.client.get(
                    indexer.url,
                    params={
                        "t": "search",
                        "q": search_query,
                        "apikey": self.api_key,
                        "cat": 6000,
                    },
                )
                response.raise_for_status()
                payload = xmltodict.parse(response.text)
            except Exception as exc:
                logger.warning(
                    "Jackett search failed movie_number={} indexer={} detail={}",
                    movie_number,
                    indexer.name,
                    exc,
                )
                raise JackettClientError(str(exc)) from exc

            channel = self._coerce_mapping((payload.get("rss") or {}).get("channel"))
            channel_title = self._coerce_text(channel.get("title"))
            for item in self._coerce_items(channel.get("item")):
                candidates.append(
                    self._build_candidate(movie_number, indexer, item, channel_title=channel_title)
                )

        candidates.sort(key=lambda item: (item.seeders, item.size_bytes), reverse=True)
        return candidates

    @classmethod
    def _build_search_query(cls, movie_number: str) -> str:
        normalized = normalize_movie_number(movie_number)
        # FC2 资源在 Jackett 中通常按纯数字检索，命中率更稳定。
        if normalized.startswith("FC2"):
            matched = cls.FC2_QUERY_PATTERN.match(normalized)
            if matched:
                return matched.group(1)
        return movie_number

    def _build_candidate(
        self,
        movie_number: str,
        indexer: Indexer,
        item: dict,
        *,
        channel_title: str = "",
    ) -> DownloadCandidateResource:
        attr_map = self._coerce_attr_map(item.get("torznab:attr"))
        remote_indexer = self._extract_indexer_metadata(item, channel_title)
        size_bytes = self._coerce_int(item.get("size"))
        magnet_url = self._coerce_text(attr_map.get("magneturl"))
        seeders = self._coerce_int(attr_map.get("seeders"))
        title = self._coerce_text(item.get("title"))
        description = self._coerce_text(item.get("description"))
        full_title = " ".join(part for part in [title, description] if part)
        return DownloadCandidateResource(
            source="jackett",
            indexer_name=indexer.name or remote_indexer["id"] or remote_indexer["name"],
            indexer_kind=indexer.kind,
            resolved_client_id=indexer.download_client_id,
            resolved_client_name=indexer.download_client.name,
            movie_number=movie_number.upper(),
            title=full_title or title,
            size_bytes=size_bytes,
            seeders=seeders,
            magnet_url=magnet_url,
            torrent_url=self._coerce_text(item.get("link") or item.get("guid")),
            tags=detect_candidate_tags(full_title or title, movie_number, size_bytes),
        )

    @staticmethod
    def _coerce_mapping(value) -> dict:
        return value if isinstance(value, dict) else {}

    @classmethod
    def _coerce_items(cls, value) -> list[dict]:
        if value is None:
            return []
        items = [value] if isinstance(value, dict) else value
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @classmethod
    def _coerce_attr_map(cls, value) -> dict[str, str]:
        attrs = cls._coerce_items(value)
        attr_map: dict[str, str] = {}
        for attr in attrs:
            name = cls._coerce_text(attr.get("@name"))
            if not name:
                continue
            attr_map[name] = cls._coerce_text(attr.get("@value"))
        return attr_map

    @classmethod
    def _extract_indexer_metadata(cls, item: dict, channel_title: str) -> dict[str, str]:
        jackett_indexer = cls._coerce_mapping(item.get("jackettindexer"))
        plain_indexer = cls._coerce_mapping(item.get("indexer"))
        return {
            "id": cls._coerce_text(jackett_indexer.get("@id") or plain_indexer.get("@id")),
            "name": (
                cls._coerce_text(jackett_indexer.get("#text"))
                or cls._coerce_text(plain_indexer.get("#text"))
                or cls._coerce_text(item.get("jackettindexer"))
                or cls._coerce_text(item.get("indexer"))
                or channel_title
            ),
        }

    @staticmethod
    def _coerce_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            text_value = value.get("#text")
            if isinstance(text_value, str):
                return text_value.strip()
            return ""
        return str(value).strip()

    @staticmethod
    def _coerce_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
