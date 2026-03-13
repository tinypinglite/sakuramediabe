from typing import List, Optional

import httpx
import xmltodict
from loguru import logger

from src.config.config import IndexerKind, settings
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.tag_rules import detect_candidate_tags


class JackettClientError(Exception):
    pass


class JackettClient:
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
        for indexer in settings.indexer_settings.indexers:
            if normalized_kind and indexer.kind.value != normalized_kind:
                continue
            try:
                response = self.client.get(
                    indexer.url,
                    params={
                        "t": "search",
                        "q": movie_number,
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

            items = payload.get("rss", {}).get("channel", {}).get("item") or []
            if isinstance(items, dict):
                items = [items]
            for item in items:
                candidates.append(
                    self._build_candidate(movie_number, indexer.name, indexer.kind, item)
                )

        candidates.sort(key=lambda item: (item.seeders, item.size_bytes), reverse=True)
        return candidates

    def _build_candidate(
        self,
        movie_number: str,
        indexer_name: str,
        indexer_kind: IndexerKind,
        item: dict,
    ) -> DownloadCandidateResource:
        attrs = item.get("torznab:attr") or []
        if isinstance(attrs, dict):
            attrs = [attrs]
        attr_map = {attr.get("@name"): attr.get("@value") for attr in attrs if isinstance(attr, dict)}
        size_bytes = int(item.get("size") or 0)
        magnet_url = attr_map.get("magneturl") or ""
        seeders = int(attr_map.get("seeders") or 0)
        title = (item.get("title") or "").strip()
        description = (item.get("description") or "").strip()
        full_title = f"{title} {description}".strip()
        return DownloadCandidateResource(
            source="jackett",
            indexer_name=(item.get("jackettindexer") or item.get("indexer") or "").strip()
            or indexer_name,
            indexer_kind=indexer_kind.value,
            movie_number=movie_number.upper(),
            title=full_title or title,
            size_bytes=size_bytes,
            seeders=seeders,
            magnet_url=magnet_url,
            torrent_url=(item.get("link") or "").strip(),
            tags=detect_candidate_tags(full_title or title, movie_number, size_bytes),
        )
