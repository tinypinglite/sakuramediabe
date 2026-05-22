from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import requests as cffi_requests
from lxml import html as lxml_html
from loguru import logger

from .exceptions import MissavRankingRequestError, MissavThumbnailNotFoundError, MissavThumbnailRequestError
from .models import MissavThumbnailManifest
from .utils import normalize_movie_number, parse_movie_number_from_text


class MissavThumbnailProvider:
    PAGE_URL_TEMPLATE = "https://missav.ws/cn/{movie_number}"
    PAGE_REQUEST_TIMEOUT_SECONDS = 30
    PAGE_REQUEST_IMPERSONATE = "chrome124"
    CONFIG_PATTERN = re.compile(r"previewthumbs\s*:\s*(?P<body>\{.*?\})\s*,\s*server\s*:", re.DOTALL)
    SERVER_PATTERN = re.compile(r"server\s*:\s*[\"'](?P<server>[^\"']+)[\"']")

    def __init__(self, *, proxy: str | None = None):
        self.proxy = proxy
        self._session = cffi_requests.Session(
            impersonate=self.PAGE_REQUEST_IMPERSONATE,
            proxy=proxy,
        )

    def build_page_headers(self) -> dict[str, str]:
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "upgrade-insecure-requests": "1",
        }

    def _http_get(self, url: str):
        response = self._session.get(
            url,
            headers=self.build_page_headers(),
            timeout=self.PAGE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response

    def build_movie_page_url(self, movie_number: str) -> str:
        return self.PAGE_URL_TEMPLATE.format(movie_number=normalize_movie_number(movie_number))

    def fetch_thumbnail_manifest(self, movie_number: str) -> MissavThumbnailManifest:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            raise MissavThumbnailNotFoundError(movie_number, "movie number is invalid")
        page_url = self.build_movie_page_url(normalized_movie_number)
        try:
            response = self._http_get(page_url)
        except Exception as exc:
            raise MissavThumbnailRequestError(page_url, str(exc)) from exc
        return self._parse_thumbnail_manifest(response.text, movie_number=normalized_movie_number, page_url=page_url)

    def _parse_thumbnail_manifest(self, html: str, *, movie_number: str, page_url: str) -> MissavThumbnailManifest:
        config_match = self.CONFIG_PATTERN.search(html)
        server_match = self.SERVER_PATTERN.search(html)
        if config_match is None or server_match is None:
            raise MissavThumbnailNotFoundError(movie_number, "previewthumbs config not found")
        block = config_match.group("body")
        urls = self._extract_urls(block)
        if not urls:
            raise MissavThumbnailNotFoundError(movie_number, "thumbnail urls not found")
        server = server_match.group("server").strip().rstrip("/")
        resolved_urls = [url if url.startswith("http") else f"{server}/{url.lstrip('/')}" for url in urls]
        return MissavThumbnailManifest(
            movie_number=movie_number,
            page_url=page_url,
            pic_num=self._extract_int_field(block, "pic_num"),
            width=self._extract_int_field(block, "width"),
            height=self._extract_int_field(block, "height"),
            col=self._extract_int_field(block, "col"),
            row=self._extract_int_field(block, "row"),
            offset_x=self._extract_int_field(block, "offset_x"),
            offset_y=self._extract_int_field(block, "offset_y"),
            urls=resolved_urls,
        )

    @staticmethod
    def _extract_int_field(block: str, field_name: str) -> int:
        match = re.search(rf"{re.escape(field_name)}\s*:\s*(\d+)", block)
        return int(match.group(1)) if match else 0

    @classmethod
    def _extract_urls(cls, block: str) -> list[str]:
        match = re.search(r"urls\s*:\s*\[(?P<body>.*?)\]", block, re.DOTALL)
        if match is None:
            return []
        return [cls._decode_js_string(item) for item in re.findall(r"[\"']((?:\\.|[^\"'])+)[\"']", match.group("body"))]

    @staticmethod
    def _decode_js_string(value: str) -> str:
        try:
            return bytes(value, "utf-8").decode("unicode_escape")
        except Exception:
            return value


class MissavRankingProvider(MissavThumbnailProvider):
    RANKING_MAX_PAGES = 6
    RANKING_URLS = {
        "daily": "https://missav.ws/dm534/cn/new?sort=today_views",
        "weekly": "https://missav.ws/dm534/cn/new?sort=weekly_views",
        "monthly": "https://missav.ws/dm534/cn/new?sort=monthly_views",
    }
    RANKING_LIST_XPATH = (
        '//div[contains(concat(" ", normalize-space(@class), " "), " grid ")]'
        '[.//div[contains(concat(" ", normalize-space(@class), " "), " thumbnail ")]]'
    )
    RANKING_CARD_XPATH = './/div[contains(concat(" ", normalize-space(@class), " "), " thumbnail ")]'
    RANKING_CARD_LINK_XPATH = (
        './/a[@href][not(starts-with(@href, "#"))]'
        '[not(starts-with(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "javascript:"))]/@href'
    )
    LEGACY_CARD_LINK_XPATH = '//a[contains(concat(" ", normalize-space(@class), " "), " thumbnail ")][@href]/@href'

    def build_ranking_url(self, period: str, page: int = 1) -> str:
        try:
            base_url = self.RANKING_URLS[period]
        except KeyError as exc:
            raise ValueError(f"unsupported missav ranking period: {period}") from exc
        url_parts = urlsplit(base_url)
        query_items = [(key, value) for key, value in parse_qsl(url_parts.query, keep_blank_values=True) if key != "page"]
        query_items.append(("page", str(page)))
        return urlunsplit(url_parts._replace(query=urlencode(query_items)))

    def fetch_rank_numbers(self, period: str) -> list[str]:
        numbers: list[str] = []
        seen: set[str] = set()
        for page in range(1, self.RANKING_MAX_PAGES + 1):
            page_url = self.build_ranking_url(period, page=page)
            try:
                response = self._http_get(page_url)
            except Exception as exc:
                raise MissavRankingRequestError(page_url, str(exc)) from exc
            for number in self._parse_rank_numbers(response.text, page_url=page_url):
                if number in seen:
                    continue
                seen.add(number)
                numbers.append(number)
        return numbers

    def _parse_rank_numbers(self, html: str, *, page_url: str) -> list[str]:
        numbers: list[str] = []
        seen: set[str] = set()
        for raw_number in self._extract_rank_number_candidates(html):
            number = normalize_movie_number(raw_number)
            if not number or number in seen:
                continue
            seen.add(number)
            numbers.append(number)
        if not numbers:
            logger.warning("MissAV ranking parse found no cards page_url={}", page_url)
            raise MissavRankingRequestError(page_url, "ranking cards not found")
        return numbers

    def _extract_rank_number_candidates(self, html: str) -> list[str]:
        if not html.strip():
            return []
        try:
            root = lxml_html.fromstring(html)
        except Exception:
            return []

        candidates: list[str] = []
        list_containers = root.xpath(self.RANKING_LIST_XPATH)
        if list_containers:
            for card in list_containers[0].xpath(self.RANKING_CARD_XPATH):
                number = self._extract_first_rank_number_from_card(card)
                if number:
                    candidates.append(number)
            return candidates

        for href in root.xpath(self.LEGACY_CARD_LINK_XPATH):
            number = self._extract_rank_number_from_href(href)
            if number:
                candidates.append(number)
        return candidates

    def _extract_first_rank_number_from_card(self, card) -> str:
        for href in card.xpath(self.RANKING_CARD_LINK_XPATH):
            number = self._extract_rank_number_from_href(href)
            if number:
                return number
        return ""

    @staticmethod
    def _extract_rank_number_from_href(href: str) -> str:
        url_parts = urlsplit((href or "").strip())
        if url_parts.netloc and "missav" not in url_parts.netloc.lower():
            return ""
        url_path = url_parts.path
        slug = url_path.rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            return ""
        parsed_number = parse_movie_number_from_text(slug)
        if parsed_number:
            return parsed_number
        return slug if any(character.isdigit() for character in slug) else ""
