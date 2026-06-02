"""QBittorrentClient.add_candidate 的来源判定单元测试。

重点覆盖：磁力链可能被索引器放进 torrent_url 字段，需按内容而非字段名判定来源，
否则磁力链会被当成 .torrent 文件去 HTTP 下载，触发 "unsupported protocol 'magnet://'"。
"""

import pytest

from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError

# 40 位 btih 十六进制串，配合 parse_hash_from_magnet 的正则使用
MAGNET_HASH = "abcdef0123456789abcdef0123456789abcdef01"


class FakeTorrent:
    """模拟 qbittorrent-api 返回的种子对象，覆盖 _to_dict 读取的属性。"""

    def __init__(self, info_hash: str):
        self.hash = info_hash
        self.name = "movie"
        self.progress = 0.0
        self.state = "queuedDL"
        self.content_path = "/downloads/movie"
        self.save_path = "/downloads"
        self.tags = ""


class FakeQbClient:
    """记录 torrents_add 调用参数的假 qbittorrent-api 客户端。"""

    def __init__(self):
        self.add_calls = []

    def auth_log_in(self):
        pass

    def torrents_add(self, **kwargs):
        self.add_calls.append(kwargs)
        return "Ok."

    def torrents_add_tags(self, **kwargs):  # 仅 409 幂等分支会用到
        pass

    def torrents_info(self, torrent_hashes=None, tag=None):
        info_hash = (torrent_hashes or [""])[0]
        return [FakeTorrent(info_hash)]


class FakeHttpResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class FakeHttpClient:
    """记录 get 调用 URL 的假 httpx 客户端，用于断言是否发生了 HTTP 下载。"""

    def __init__(self):
        self.get_calls = []

    def get(self, url):
        self.get_calls.append(url)
        return FakeHttpResponse(b"placeholder-torrent-bytes")


def _make_client(qb=None, http=None) -> QBittorrentClient:
    return QBittorrentClient(
        base_url="http://qb:8080",
        username="u",
        password="p",
        client=qb or FakeQbClient(),
        http_client=http or FakeHttpClient(),
    )


def test_add_candidate_routes_magnet_in_torrent_url_field():
    # 磁力链落在 torrent_url 字段时，应走磁力分支，绝不能去 HTTP 下载
    qb = FakeQbClient()
    http = FakeHttpClient()
    client = _make_client(qb, http)

    result = client.add_candidate(
        magnet_url="",
        torrent_url=f"magnet://xt=urn:btih:{MAGNET_HASH.upper()}&dn=demo",
        save_path="/downloads",
        rename="MIAB-317",
        client_id=1,
    )

    assert http.get_calls == []
    assert len(qb.add_calls) == 1
    assert qb.add_calls[0]["urls"] == f"magnet:?xt=urn:btih:{MAGNET_HASH.upper()}&dn=demo"
    assert "torrent_files" not in qb.add_calls[0]
    assert result["info_hash"] == MAGNET_HASH


def test_add_candidate_normalizes_double_slash_magnet():
    # "magnet://" 非标准写法应被规范化为标准的 "magnet:?"
    qb = FakeQbClient()
    http = FakeHttpClient()
    client = _make_client(qb, http)

    client.add_candidate(
        magnet_url=f"magnet://xt=urn:btih:{MAGNET_HASH}",
        torrent_url="",
        save_path="/downloads",
        rename="MIAB-317",
        client_id=1,
    )

    assert http.get_calls == []
    assert qb.add_calls[0]["urls"] == f"magnet:?xt=urn:btih:{MAGNET_HASH}"


def test_add_candidate_normalizes_double_slash_question_magnet():
    # "magnet://?xt=" 应规范化为 "magnet:?xt="，不能出现重复的问号
    qb = FakeQbClient()
    client = _make_client(qb)

    client.add_candidate(
        magnet_url="",
        torrent_url=f"magnet://?xt=urn:btih:{MAGNET_HASH}",
        save_path="/downloads",
        rename="MIAB-317",
        client_id=1,
    )

    assert qb.add_calls[0]["urls"] == f"magnet:?xt=urn:btih:{MAGNET_HASH}"


def test_add_candidate_downloads_real_http_torrent_file(monkeypatch):
    # 真正的 http(s) 种子文件地址仍应走下载分支
    qb = FakeQbClient()
    http = FakeHttpClient()
    client = _make_client(qb, http)
    monkeypatch.setattr(
        QBittorrentClient,
        "parse_hash_from_torrent",
        staticmethod(lambda _bytes: MAGNET_HASH),
    )

    client.add_candidate(
        magnet_url="",
        torrent_url="http://indexer.local/download/x.torrent",
        save_path="/downloads",
        rename="MIAB-317",
        client_id=1,
    )

    assert http.get_calls == ["http://indexer.local/download/x.torrent"]
    assert "torrent_files" in qb.add_calls[0]
    assert "urls" not in qb.add_calls[0]


def test_add_candidate_prefers_magnet_over_torrent_file():
    # 同时提供磁力链与种子文件地址时，优先使用磁力链
    qb = FakeQbClient()
    http = FakeHttpClient()
    client = _make_client(qb, http)

    client.add_candidate(
        magnet_url=f"magnet:?xt=urn:btih:{MAGNET_HASH}",
        torrent_url="http://indexer.local/download/x.torrent",
        save_path="/downloads",
        rename="MIAB-317",
        client_id=1,
    )

    assert http.get_calls == []
    assert qb.add_calls[0]["urls"] == f"magnet:?xt=urn:btih:{MAGNET_HASH}"


def test_add_candidate_requires_source():
    # 磁力链与种子文件地址都为空时应明确报错
    client = _make_client()
    with pytest.raises(QBittorrentClientError):
        client.add_candidate(
            magnet_url="",
            torrent_url="",
            save_path="/downloads",
            rename="MIAB-317",
            client_id=1,
        )


class _FileNoIndex:
    """模拟旧版 qB（<4.4.0）文件对象：不返回 index 字段。"""

    def __init__(self, name, size, priority):
        self.name = name
        self.size = size
        self.priority = priority


def test_list_torrent_files_falls_back_to_position_when_index_missing():
    # 旧版 qB 文件列表不含 index，应回退用列表位置，绝不能让所有文件 index 都退化成 0
    class FakeQb:
        def auth_log_in(self):
            pass

        def torrents_files(self, torrent_hash=None):
            return [
                _FileNoIndex("a.mkv", 100, 1),
                _FileNoIndex("b.mkv", 200, 0),
                _FileNoIndex("c.mkv", 300, 1),
            ]

    client = _make_client(qb=FakeQb())
    files = client.list_torrent_files("hash-x")

    assert [f["index"] for f in files] == [0, 1, 2]
    assert [f["name"] for f in files] == ["a.mkv", "b.mkv", "c.mkv"]
    assert [f["priority"] for f in files] == [1, 0, 1]
