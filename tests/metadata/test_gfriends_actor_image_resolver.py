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


def test_resolve_uses_fresh_local_cache_without_remote_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text(
        '{"name":"root","type":"directory","children":[{"name":"三上悠亚.jpg","type":"file","fullPath":"女优头像/三上悠亚.jpg"}]}',
        encoding="utf-8",
    )

    def _unexpected_fetch(method: str, url: str):
        raise AssertionError("remote fetch should not be called when cache is fresh")

    monkeypatch.setattr(resolver, "request_json", _unexpected_fetch)

    url = resolver.resolve(["三上悠亚"])

    assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"


def test_resolve_fetches_remote_filetree_and_writes_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    payload = _build_filetree_payload()
    called = {"count": 0}

    def _fetch(method: str, url: str):
        called["count"] += 1
        assert method == "GET"
        assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
        return payload

    monkeypatch.setattr(resolver, "request_json", _fetch)

    url = resolver.resolve(["三上悠亚"])

    assert called["count"] == 1
    assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"
    assert resolver.cache_path.exists()
    assert '"fullPath": "女优头像/三上悠亚.jpg"' in resolver.cache_path.read_text(encoding="utf-8")


def test_resolve_uses_stale_cache_when_remote_refresh_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text(
        '{"name":"root","type":"directory","children":[{"name":"桥本有菜.png","type":"file","fullPath":"女优头像/nested/桥本有菜.png"}]}',
        encoding="utf-8",
    )
    current_timestamp = resolver.cache_path.stat().st_mtime + resolver.cache_ttl_seconds + 20
    monkeypatch.setattr("src.metadata.gfriends.time.time", lambda: current_timestamp)

    def _broken_fetch(method: str, url: str):
        raise RuntimeError("network down")

    monkeypatch.setattr(resolver, "request_json", _broken_fetch)

    url = resolver.resolve(["桥本有菜"])

    assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/nested/桥本有菜.png"


def test_resolve_matches_name_name_zht_and_other_name_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    payload = _build_filetree_payload()
    monkeypatch.setattr(resolver, "request_json", lambda method, url: payload)

    assert resolver.resolve(["相泽南"]) == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/相泽南.webp"
    assert resolver.resolve(["桥本有菜", "三上悠亚"]) == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/nested/桥本有菜.png"
    assert resolver.resolve(["  三上  悠亚  "]) == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/女优头像/三上悠亚.jpg"


def test_resolve_returns_none_when_no_actor_image_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    monkeypatch.setattr(resolver, "request_json", lambda method, url: _build_filetree_payload())

    assert resolver.resolve(["不存在女优"]) is None


def test_resolve_supports_actual_gfriends_content_mapping_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    resolver = _build_resolver(tmp_path, monkeypatch)
    monkeypatch.setattr(resolver, "request_json", lambda method, url: _build_mapping_filetree_payload())

    url = resolver.resolve(["三上悠亚"])

    assert url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Content/z-ラグジュTV/AI-Fix-三上悠亚.jpg?t=1607433809"


def test_gfriends_resolver_uses_longer_timeout_than_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    created_kwargs = []

    class FakeHttpClient:
        def __init__(self, **kwargs: Dict[str, Any]):
            self.kwargs = kwargs

    def _fake_client(**kwargs: Dict[str, Any]):
        created_kwargs.append(kwargs)
        return FakeHttpClient(**kwargs)

    monkeypatch.setattr("src.metadata.provider.httpx.Client", _fake_client)

    GfriendsActorImageResolver(
        filetree_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json",
        cdn_base_url="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends",
        cache_path=str(tmp_path / "gfriends-filetree.json"),
        cache_ttl_hours=168,
    )

    assert created_kwargs[0]["timeout"] == 60.0

