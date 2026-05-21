from __future__ import annotations

import html
import json
import re
from urllib.parse import quote

from .utils import normalize_movie_number, parse_movie_number_from_text
from .exceptions import MetadataNotFoundError
from .http_client import MetadataRequestClient


class DmmMovieNumberNotFoundError(MetadataNotFoundError):
    def __init__(self, movie_number: str):
        self.resource = "movie_desc"
        self.lookup_value = movie_number
        Exception.__init__(self, f"DMM 未找到对应番号: {movie_number}")


class DmmMovieDescNotFoundError(MetadataNotFoundError):
    def __init__(self, movie_number: str):
        self.resource = "movie_desc"
        self.lookup_value = movie_number
        Exception.__init__(self, f"DMM 已找到对应番号但详情页没有描述: {movie_number}")


class DmmProvider(MetadataRequestClient):
    def __init__(self, *, proxy: str | None = None):
        MetadataRequestClient.__init__(
            self,
            proxy=proxy,
        )

    SEARCH_URL_TEMPLATE = "https://www.dmm.co.jp/search/=/searchstr={movie_number}/limit=30/sort=date/"
    BACKEND_DETAIL_URL_PATTERN = re.compile(
        r'"(?:detailUrl|detail_url)"\s*:\s*"(?P<url>[^"]+)"',
        re.IGNORECASE,
    )
    ANCHOR_DETAIL_URL_PATTERN = re.compile(r'href="(?P<url>https://www\.dmm\.co\.jp/[^"]+?/detail/=/cid=[^"]+)"')
    CID_PATTERN = re.compile(r"cid=([^/?&#]+)")
    LD_JSON_PATTERN = re.compile(
        r'<script[^>]+type="application/ld\+json"[^>]*>\s*(?P<body>.*?)\s*</script>',
        re.IGNORECASE | re.DOTALL,
    )
    MONO_DESC_BLOCK_PATTERN = re.compile(
        r'<div class="mg-b20 lh4">\s*(?P<body>.*?)\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )
    MONO_DESC_PARAGRAPH_PATTERN = re.compile(
        r'<p class="mg-b20">\s*(?P<body>.*?)\s*</p>',
        re.IGNORECASE | re.DOTALL,
    )
    TAG_PATTERN = re.compile(r"<[^>]+>")

    def get_movie_desc(self, movie_number: str) -> str:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            raise DmmMovieNumberNotFoundError(movie_number)

        search_html = self.request_text(
            "GET",
            self.SEARCH_URL_TEMPLATE.format(movie_number=quote(movie_number.strip())),
        )
        matched_detail_urls = self._match_detail_urls(search_html, normalized_movie_number)
        if not matched_detail_urls:
            raise DmmMovieNumberNotFoundError(movie_number)

        for detail_url in matched_detail_urls[::-1]:
            detail_html = self.request_text("GET", detail_url)
            description = self._extract_detail_description(detail_url, detail_html)
            if description:
                return description
        raise DmmMovieDescNotFoundError(movie_number)

    @classmethod
    def _match_detail_url(cls, search_html: str, normalized_movie_number: str) -> str | None:
        matched_detail_urls = cls._match_detail_urls(search_html, normalized_movie_number)
        if not matched_detail_urls:
            return None
        return matched_detail_urls[0]

    @classmethod
    def _match_detail_urls(cls, search_html: str, normalized_movie_number: str) -> list[str]:
        matched_detail_urls: list[str] = []
        for detail_url in cls.extract_detail_urls(search_html):
            cid = cls.extract_cid(detail_url)
            if not cid:
                continue
            parsed_movie_number = normalize_movie_number(parse_movie_number_from_text(cid))
            if parsed_movie_number == normalized_movie_number:
                matched_detail_urls.append(detail_url)
        return matched_detail_urls

    @classmethod
    def extract_detail_urls(cls, search_html: str) -> list[str]:
        urls: list[str] = []
        seen_urls: set[str] = set()
        for pattern in (cls.BACKEND_DETAIL_URL_PATTERN, cls.ANCHOR_DETAIL_URL_PATTERN):
            for match in pattern.finditer(search_html):
                detail_url = cls._decode_detail_url(match.group("url"))
                if "https://www.dmm.co.jp/" not in detail_url or "/detail/=/cid=" not in detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                urls.append(detail_url)
        return urls

    @classmethod
    def extract_cid(cls, detail_url: str) -> str:
        match = cls.CID_PATTERN.search(detail_url)
        if match is None:
            return ""
        return html.unescape(match.group(1)).strip()

    @classmethod
    def _extract_detail_description(cls, detail_url: str, detail_html: str) -> str:
        if "/rental/" in detail_url:
            return cls.extract_rental_description(detail_html)
        return cls.extract_mono_description(detail_html)

    @classmethod
    def extract_rental_description(cls, detail_html: str) -> str:
        for match in cls.LD_JSON_PATTERN.finditer(detail_html):
            try:
                payload = json.loads(match.group("body"))
            except json.JSONDecodeError:
                continue
            description = cls._find_description_in_ld_json(payload)
            if description:
                return description
        return ""

    @classmethod
    def _find_description_in_ld_json(cls, payload) -> str:
        if isinstance(payload, list):
            for item in payload:
                description = cls._find_description_in_ld_json(item)
                if description:
                    return description
            return ""
        if not isinstance(payload, dict):
            return ""
        graph_payload = payload.get("@graph")
        if graph_payload is not None:
            description = cls._find_description_in_ld_json(graph_payload)
            if description:
                return description
        type_value = str(payload.get("@type", "")).strip().lower()
        if type_value == "product":
            description = cls._normalize_description(payload.get("description"))
            if description:
                return description
        for value in payload.values():
            description = cls._find_description_in_ld_json(value)
            if description:
                return description
        return ""

    @classmethod
    def extract_mono_description(cls, detail_html: str) -> str:
        block_match = cls.MONO_DESC_BLOCK_PATTERN.search(detail_html)
        raw_block = block_match.group("body") if block_match is not None else detail_html
        paragraph_match = cls.MONO_DESC_PARAGRAPH_PATTERN.search(raw_block)
        if paragraph_match is not None:
            return cls._strip_html(paragraph_match.group("body"))
        if block_match is None:
            return ""
        return cls._strip_html(raw_block)

    @staticmethod
    def _decode_detail_url(value: str) -> str:
        decoded = (
            value.replace("\\/", "/")
            .replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u002f", "/")
        )
        return html.unescape(decoded)

    @staticmethod
    def _normalize_description(value: str | None) -> str:
        return html.unescape((value or "")).strip()

    @classmethod
    def _strip_html(cls, value: str | None) -> str:
        plain_text = cls.TAG_PATTERN.sub("", value or "")
        return cls._normalize_description(re.sub(r"\s+", " ", plain_text))

    def build_request_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": "age_check_done=1; ckcy=1",
        }
