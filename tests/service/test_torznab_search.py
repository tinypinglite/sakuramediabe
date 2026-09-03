from types import SimpleNamespace

import httpx
import pytest

from src.model import Indexer
from src.service.transfers.downloads.clients.torznab import (
    TorznabClient,
    TorznabClientError,
)
from src.service.transfers.downloads.search_service import DownloadSearchService

TORZNAB_RESPONSE = """
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>good-indexer</title>
    <item>
      <title>SSNI-001 1080p</title>
      <size>1024</size>
      <torznab:attr name="seeders" value="5" />
      <link>magnet:?xt=urn:btih:good-result</link>
    </item>
  </channel>
</rss>
"""

TORZNAB_HTML_TITLE_RESPONSE = """
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>good-indexer</title>
    <item>
      <title>SSNI-001</title>
      <description>uncensored&lt;/br&gt; 1080p &amp;amp; remux</description>
      <size>1024</size>
      <torznab:attr name="seeders" value="5" />
      <link>magnet:?xt=urn:btih:good-result</link>
    </item>
  </channel>
</rss>
"""


class _IndexerQuery:
    def __init__(self, indexers):
        self.indexers = indexers

    def order_by(self, *_args):
        return self

    def __iter__(self):
        return iter(self.indexers)


class _FakeHttpClient:
    def __init__(self, failing_urls=None):
        self.calls: list[str] = []
        self.failing_urls = failing_urls or {"http://bad-indexer/api"}

    def get(self, url, *, params):
        self.calls.append(url)
        if url in self.failing_urls:
            raise httpx.ConnectError(
                "connection failed",
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            text=TORZNAB_RESPONSE,
            request=httpx.Request("GET", url, params=params),
        )


def _patch_indexers(monkeypatch):
    bad_indexer = SimpleNamespace(
        id=1,
        name="bad-indexer",
        url="http://bad-indexer/api",
        kind="pt",
        api_key=None,
    )
    good_indexer = SimpleNamespace(
        id=2,
        name="good-indexer",
        url="http://good-indexer/api",
        kind="pt",
        api_key=None,
    )
    download_clients = {
        1: [SimpleNamespace(id=11, name="bad-client")],
        2: [SimpleNamespace(id=22, name="good-client")],
    }
    monkeypatch.setattr(
        Indexer,
        "select",
        lambda *_args: _IndexerQuery([bad_indexer, good_indexer]),
    )
    monkeypatch.setattr(
        TorznabClient,
        "_load_clients_by_indexer",
        staticmethod(lambda: download_clients),
    )


def test_download_search_returns_results_when_one_indexer_fails(monkeypatch):
    _patch_indexers(monkeypatch)
    http_client = _FakeHttpClient()

    candidates = DownloadSearchService(
        TorznabClient(client=http_client),
    ).search_candidates(movie_number="SSNI-001")

    assert len(candidates) == 1
    assert candidates[0].indexer_name == "good-indexer"
    assert http_client.calls == [
        "http://bad-indexer/api",
        "http://good-indexer/api",
    ]


def test_torznab_search_remains_strict_by_default(monkeypatch):
    _patch_indexers(monkeypatch)
    http_client = _FakeHttpClient()

    with pytest.raises(TorznabClientError):
        TorznabClient(client=http_client).search("SSNI-001")

    assert http_client.calls == ["http://bad-indexer/api"]


def test_torznab_search_still_reports_when_all_indexers_fail(monkeypatch):
    _patch_indexers(monkeypatch)
    http_client = _FakeHttpClient(
        failing_urls={
            "http://bad-indexer/api",
            "http://good-indexer/api",
        }
    )

    with pytest.raises(TorznabClientError):
        TorznabClient(client=http_client).search(
            "SSNI-001",
            continue_on_error=True,
        )

    assert http_client.calls == [
        "http://bad-indexer/api",
        "http://good-indexer/api",
    ]


def test_torznab_search_strips_html_from_candidate_title(monkeypatch):
    _patch_indexers(monkeypatch)

    class HtmlTitleClient(_FakeHttpClient):
        def get(self, url, *, params):
            self.calls.append(url)
            if url == "http://bad-indexer/api":
                raise httpx.ConnectError(
                    "connection failed",
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                text=TORZNAB_HTML_TITLE_RESPONSE,
                request=httpx.Request("GET", url, params=params),
            )

    candidates = DownloadSearchService(TorznabClient(client=HtmlTitleClient())).search_candidates(
        movie_number="SSNI-001"
    )

    assert candidates[0].title == "SSNI-001 uncensored 1080p & remux"
