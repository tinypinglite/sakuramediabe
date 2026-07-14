from pathlib import Path
from typing import Any, Dict

import pytest

from src.metadata.gfriends import GfriendsActorImageResolver


def _build_filetree_payload() -> Dict[str, Any]:
    return {
        "name": "root",
        "type": "directory",
        "children": [
            {
                "name": "女优头像",
                "type": "directory",
                "children": [
                    {
                        "name": "三上悠亚.jpg",
                        "type": "file",
                        "fullPath": "女优头像/三上悠亚.jpg",
                    },
                    {
                        "name": "相泽南.webp",
                        "type": "file",
                        "fullPath": "女优头像/相泽南.webp",
                    },
                    {
                        "name": "nested",
                        "type": "directory",
                        "children": [
                            {
                                "name": "桥本有菜.png",
                                "type": "file",
                                "fullPath": "女优头像/nested/桥本有菜.png",
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _build_mapping_filetree_payload() -> Dict[str, Any]:
    return {
        "Content": {
            "z-ラグジュTV": {
                "三上悠亚.jpg": "AI-Fix-三上悠亚.jpg?t=1607433809",
                "Mikami Yua.jpg": "AI-Fix-三上悠亚.jpg?t=1607433809",
            },
            "MOODYZ": {
                "桥本有菜.png": "AI-Fix-桥本有菜.png?t=1607433810",
            },
        },
        "Information": {
            "TotalNum": 3,
        },
    }


def _build_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GfriendsActorImageResolver:
    cache_path = tmp_path / "gfriends-filetree.json"
    resolver = GfriendsActorImageResolver(
        filetree_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json",
        cdn_base_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends",
        cache_path=str(cache_path),
        cache_ttl_hours=168,
    )
    monkeypatch.setattr(resolver, "build_request_headers", lambda: {})
    return resolver


# ---------------------------------------------------------------------------
# resolve() 契约：只读内存 index，永不发网络请求
# ---------------------------------------------------------------------------


def test_resolve_never_calls_network_even_when_index_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("resolve() must not trigger network requests")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    # 无内存 index、无 disk cache：返回 None，不阻塞、不抛异常
    assert resolver.resolve(["三上悠亚"]) is None


def test_resolve_hydrates_from_disk_cache_when_index_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text(
        '{"name":"root","type":"directory","children":['
        '{"name":"三上悠亚.jpg","type":"file","fullPath":"女优头像/三上悠亚.jpg"}]}',
        encoding="utf-8",
    )

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("resolve() must not trigger network requests")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    assert (
        resolver.resolve(["三上悠亚"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"
    )


def test_resolve_returns_none_when_no_actor_image_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver._index = resolver._build_index(_build_filetree_payload())

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("resolve() must not trigger network requests")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    assert resolver.resolve(["不存在女优"]) is None


def test_resolve_matches_multiple_candidate_names_and_normalizes_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver._index = resolver._build_index(_build_filetree_payload())

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("resolve() must not trigger network requests")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    assert (
        resolver.resolve(["相泽南"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/相泽南.webp"
    )
    assert (
        resolver.resolve(["桥本有菜", "三上悠亚"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/nested/桥本有菜.png"
    )
    assert (
        resolver.resolve(["  三上  悠亚  "])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"
    )


def test_resolve_supports_actual_gfriends_content_mapping_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver._index = resolver._build_index(_build_mapping_filetree_payload())

    assert (
        resolver.resolve(["三上悠亚"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Content/z-ラグジュTV/AI-Fix-三上悠亚.jpg?t=1607433809"
    )


# ---------------------------------------------------------------------------
# refresh() 契约：唯一网络入口 + 缓存策略
# ---------------------------------------------------------------------------


def test_refresh_fetches_remote_filetree_and_writes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    payload = _build_filetree_payload()
    called = {"count": 0}

    def _fetch(method: str, url: str):
        called["count"] += 1
        assert method == "GET"
        assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
        return payload

    monkeypatch.setattr(resolver, "request_json", _fetch)

    stats = resolver.refresh(force=True)

    assert called["count"] == 1
    assert stats["source"] == "network"
    assert stats["entries"] == 3
    assert stats["bytes_written"] > 0
    assert stats["force"] is True
    assert resolver.cache_path.exists()
    assert '"fullPath": "女优头像/三上悠亚.jpg"' in resolver.cache_path.read_text(encoding="utf-8")

    # refresh 后 resolve 立即命中内存 index
    assert (
        resolver.resolve(["三上悠亚"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"
    )


def test_refresh_skips_network_when_cache_fresh_and_not_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text(
        '{"name":"root","type":"directory","children":['
        '{"name":"三上悠亚.jpg","type":"file","fullPath":"女优头像/三上悠亚.jpg"}]}',
        encoding="utf-8",
    )

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("refresh(force=False) must skip network when cache is fresh")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    stats = resolver.refresh(force=False)

    assert stats["source"] == "cache_fresh"
    assert stats["entries"] == 1
    assert stats["bytes_written"] == 0
    assert stats["force"] is False


def test_refresh_uses_stale_cache_when_remote_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text(
        '{"name":"root","type":"directory","children":['
        '{"name":"桥本有菜.png","type":"file","fullPath":"女优头像/nested/桥本有菜.png"}]}',
        encoding="utf-8",
    )
    # 让 cache 显得已过期，强制走网络路径
    current_timestamp = resolver.cache_path.stat().st_mtime + resolver.cache_ttl_seconds + 20
    monkeypatch.setattr("src.metadata.gfriends.time.time", lambda: current_timestamp)

    def _broken_fetch(method: str, url: str):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolver, "request_json", _broken_fetch)

    stats = resolver.refresh(force=False)

    assert stats["source"] == "stale_cache"
    assert stats["entries"] == 1
    # stale cache 已 hydrate 到内存 index，业务 resolve 立即可用
    assert (
        resolver.resolve(["桥本有菜"])
        == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/nested/桥本有菜.png"
    )


def test_refresh_raises_when_remote_fetch_fails_and_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resolver = _build_resolver(tmp_path, monkeypatch)

    def _broken_fetch(method: str, url: str):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolver, "request_json", _broken_fetch)

    with pytest.raises(RuntimeError, match="network down"):
        resolver.refresh(force=True)

    # 业务 resolve 依然安全，只是返回 None
    assert resolver.resolve(["三上悠亚"]) is None


def test_gfriends_resolver_uses_longer_timeout_than_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    created_kwargs = []

    class FakeHttpClient:
        def __init__(self, **kwargs: Dict[str, Any]):
            self.kwargs = kwargs

    def _fake_client(**kwargs: Dict[str, Any]):
        created_kwargs.append(kwargs)
        return FakeHttpClient(**kwargs)

    monkeypatch.setattr("src.metadata._providers.http_client.httpx.Client", _fake_client)

    GfriendsActorImageResolver(
        filetree_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json",
        cdn_base_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends",
        cache_path=str(tmp_path / "gfriends-filetree.json"),
        cache_ttl_hours=168,
    )

    assert created_kwargs[0]["timeout"] == 60.0
