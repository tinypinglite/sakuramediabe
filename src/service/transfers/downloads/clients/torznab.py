import re
from html.parser import HTMLParser

import httpx
import xmltodict
from loguru import logger

from src.common.movie_numbers import normalize_movie_number
from src.model import DownloadClient, Indexer, IndexerDownloadClient
from src.schema.transfers.downloads import (
    DownloadCandidateClientResource,
    DownloadCandidateResource,
)
from src.service.transfers.downloads.common import resolve_preferred_client


def _describe_search_error(exc: Exception) -> str:
    """把请求异常压成不含 URL/凭据的短描述。

    httpx 异常字符串会内嵌完整请求 URL（连同 apikey），既不能进日志也不能进
    TorznabClientError/ApiError.details；HTTP 错误保留状态码，传输层错误只保留类型 + 去 query 的地址。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        request = getattr(exc, "request", None)
        url = getattr(request, "url", None)
        if url is not None:
            return f"{type(exc).__name__} url={str(url).split('?', 1)[0]}"
    return str(exc)


class TorznabClientError(Exception):
    pass


class _CandidateTextParser(HTMLParser):
    """Turn indexer-supplied HTML fragments into plain candidate text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        del tag, attrs
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        del tag
        self.parts.append(" ")


def _clean_candidate_text(value: object) -> str:
    parser = _CandidateTextParser()
    parser.feed(TorznabClient._coerce_text(value))
    parser.close()
    return " ".join("".join(parser.parts).split())


class TorznabClient:
    FC2_QUERY_PATTERN = re.compile(r"^FC2-?(\d+)$", re.IGNORECASE)

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
    ):
        self.client = client or httpx.Client(timeout=30.0, trust_env=False)

    def search(
        self,
        movie_number: str,
        indexer_kind: str | None = None,
        *,
        continue_on_error: bool = False,
    ) -> list[DownloadCandidateResource]:
        candidates: list[DownloadCandidateResource] = []
        normalized_kind = (indexer_kind or "").strip().lower() or None
        search_query = self._build_search_query(movie_number)
        # 一趟 JOIN 取全部索引器绑定关系；候选按索引器绑定顺序解析默认 client。
        clients_by_indexer = self._load_clients_by_indexer()
        searched_indexer_count = 0
        successful_indexer_count = 0
        last_failure: tuple[str, Exception] | None = None
        for indexer in Indexer.select().order_by(Indexer.id.asc()):
            if normalized_kind and indexer.kind != normalized_kind:
                continue
            download_clients = clients_by_indexer.get(indexer.id, [])
            if not download_clients:
                # 无绑定下载器的索引器仍参与搜索没有意义：候选无法提交，直接跳过。
                logger.warning("Skip indexer without bound download clients name={}", indexer.name)
                continue
            resolved_client = resolve_preferred_client(download_clients)
            searched_indexer_count += 1
            try:
                params = {
                    "t": "search",
                    "q": search_query,
                    "cat": 6000,
                }
                # 每个索引器独立可选 key：未配置时不携带 apikey，兼容免鉴权 Torznab 端点。
                if indexer.api_key:
                    params["apikey"] = indexer.api_key
                response = self.client.get(
                    indexer.url,
                    params=params,
                )
                response.raise_for_status()
                payload = xmltodict.parse(response.text)
            except Exception as exc:
                detail = _describe_search_error(exc)
                logger.warning(
                    "Torznab search failed movie_number={} indexer={} detail={}",
                    movie_number,
                    indexer.name,
                    detail,
                )
                if continue_on_error:
                    last_failure = (detail, exc)
                    continue
                raise TorznabClientError(detail) from exc

            successful_indexer_count += 1
            channel = self._coerce_mapping((payload.get("rss") or {}).get("channel"))
            channel_title = self._coerce_text(channel.get("title"))
            for item in self._coerce_items(channel.get("item")):
                candidates.append(
                    self._build_candidate(
                        movie_number,
                        indexer,
                        item,
                        channel_title=channel_title,
                        resolved_client=resolved_client,
                        download_clients=download_clients,
                    )
                )

        # 下载候选搜索允许部分 indexer 故障，但所有可搜索 indexer 都失败时仍保留
        # 原有错误语义，避免把服务整体不可用误报成“没有搜索结果”。
        if continue_on_error and searched_indexer_count and successful_indexer_count == 0:
            detail, cause = last_failure
            raise TorznabClientError(detail) from cause

        candidates.sort(key=lambda item: (item.seeders, item.size_bytes), reverse=True)
        return candidates

    @staticmethod
    def _load_clients_by_indexer() -> dict[int, list[DownloadClient]]:
        clients_by_indexer: dict[int, list[DownloadClient]] = {}
        for link in (
            IndexerDownloadClient.select(IndexerDownloadClient, DownloadClient)
            .join(DownloadClient)
            .order_by(IndexerDownloadClient.id.asc())
        ):
            clients_by_indexer.setdefault(link.indexer_id, []).append(link.download_client)
        return clients_by_indexer

    @classmethod
    def _build_search_query(cls, movie_number: str) -> str:
        normalized = normalize_movie_number(movie_number)
        # FC2 资源在 Torznab 聚合器中通常按纯数字检索，命中率更稳定。
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
        resolved_client: DownloadClient,
        download_clients: list[DownloadClient],
    ) -> DownloadCandidateResource:
        attr_map = self._coerce_attr_map(item.get("torznab:attr"))
        remote_indexer = self._extract_indexer_metadata(item, channel_title)
        size_bytes = self._coerce_int(item.get("size"))
        seeders = self._coerce_int(attr_map.get("seeders"))
        title = _clean_candidate_text(item.get("title"))
        description = _clean_candidate_text(item.get("description"))
        full_title = " ".join(part for part in [title, description] if part)
        # Torznab 协议中磁力链可能出现在 magneturl 属性、link 或 guid 任一字段
        # （只有磁力链没有 .torrent 文件时，聚合器如 Jackett 会把磁力链直接塞进 link/guid），
        # 因此按内容而非字段名分流：磁力链归 magnet_url，其余链接归 torrent_url。
        source_uri = self._resolve_source_uri(
            attr_map.get("magneturl"),
            item.get("link"),
            item.get("guid"),
        )
        return DownloadCandidateResource(
            source_uri=source_uri,
            indexer_name=indexer.name or remote_indexer["id"] or remote_indexer["name"],
            indexer_kind=indexer.kind,
            resolved_client_id=resolved_client.id,
            resolved_client_name=resolved_client.name,
            download_clients=[
                DownloadCandidateClientResource(
                    id=download_client.id,
                    name=download_client.name,
                )
                for download_client in download_clients
            ],
            movie_number=movie_number.strip(),
            title=full_title or title,
            size_bytes=size_bytes,
            seeders=seeders,
        )

    @classmethod
    def _resolve_source_uri(cls, *raw_links) -> str:
        # The provider receives one opaque URI and decides whether it is a
        # magnet, torrent URL, or another supported source.
        magnet_url = ""
        for raw in raw_links:
            link = cls._coerce_text(raw)
            if not link:
                continue
            if link.lower().startswith("magnet:"):
                magnet_url = magnet_url or link
            else:
                return link
        return magnet_url

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
        # jackettindexer 是 Jackett 的扩展字段；Prowlarr/原生 Torznab 走 indexer 或 channel title 回退。
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
