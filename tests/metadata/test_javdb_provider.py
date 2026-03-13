from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from src.metadata.javdb import JavdbProvider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.schema.metadata.javdb import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
)


class FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http error: {self.status_code}")


class FakeActorImageResolver:
    def __init__(self, mapping=None, should_raise: bool = False):
        self.mapping = mapping or {}
        self.should_raise = should_raise
        self.calls = []

    def resolve(self, candidate_names):
        self.calls.append(candidate_names)
        if self.should_raise:
            raise RuntimeError("resolver failed")
        for name in candidate_names:
            if name in self.mapping:
                return self.mapping[name]
        return None


def build_url(host: str, path: str) -> str:
    return f"https://{host}{path}"


def install_request_stub(
    monkeypatch: pytest.MonkeyPatch,
    routes: Dict[Tuple[str, str], FakeResponse],
) -> List[Tuple[str, str, Optional[Dict[str, str]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    requests: List[
        Tuple[
            str,
            str,
            Optional[Dict[str, str]],
            Optional[Dict[str, Any]],
            Optional[Dict[str, Any]],
        ]
    ] = []

    def fake_request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> FakeResponse:
        requests.append((method, url, headers, data, params))
        key = (method.upper(), url)
        if key not in routes:
            raise AssertionError(f"unexpected request: {key}")
        return routes[key]

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    return requests


def test_build_movie_list_item_uses_metadata_schema_with_string_cover_image():
    provider = JavdbProvider(host="jdforrepam.com")

    resource = provider._build_movie_list_item(
        {
            "id": "ab12cd",
            "number": "SSNI-888",
            "title": "Matched",
            "release_date": "2024-02-14",
            "cover_url": "https://example.com/cover.jpg",
            "duration": 150,
        }
    )

    assert isinstance(resource, JavdbMovieListItemResource)
    assert resource.cover_image == "https://example.com/cover.jpg"
    assert resource.is_subscribed is None
    assert resource.model_dump(by_alias=True)["cover_image"] == "https://example.com/cover.jpg"


def test_get_actor_movies_fetches_actor_tag_movies(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    actor_movies_url = build_url(
        host,
        "/api/v1/movies/tags?filter_by=0:a:actor-1&sort_by=release&order_by=desc&page=2",
    )
    requests = install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-9",
                                "type": 0,
                                "name": "三上悠亞",
                                "avatar_url": "https://example.com/other.jpg",
                            },
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
            ("GET", actor_movies_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movies": [
                            {
                                "id": "aa11",
                                "number": "SSNI-001",
                                "title": "Newer",
                                "release_date": "2024-02-03",
                            },
                            {
                                "id": "bb22",
                                "number": "SSNI-002",
                                "title": "Older",
                                "release_date": "2024-01-02",
                            },
                        ],
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)
    result = provider.get_actor_movies("三上悠亚", page=2)

    assert isinstance(result, List)
    assert [movie.javdb_id for movie in result] == ["aa11", "bb22"]
    assert [movie.movie_number for movie in result] == ["SSNI-001", "SSNI-002"]
    assert [str(movie.release_date) for movie in result] == ["2024-02-03", "2024-01-02"]
    assert requests[0][2]["host"] == host
    assert "jdsignature" in requests[0][2]


def test_search_actor_returns_mapped_actor_resource(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)
    actor = provider.search_actor("三上悠亚")

    assert isinstance(actor, JavdbMovieActorResource)
    assert actor.javdb_id == "actor-1"
    assert actor.javdb_type == 0
    assert actor.name == "三上悠亚"
    assert actor.avatar_url == "https://example.com/actor.jpg"


def test_search_actor_prefers_gfriends_actor_image(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "name_zht": "三上悠亞",
                                "other_name": "鬼头桃菜,三上悠亚",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )
    resolver = FakeActorImageResolver({"三上悠亚": "https://cdn.example.com/mikami.jpg"})

    provider = JavdbProvider(host=host, actor_image_resolver=resolver)
    actor = provider.search_actor("三上悠亚")

    assert actor.avatar_url == "https://cdn.example.com/mikami.jpg"
    assert resolver.calls == [["三上悠亚", "三上悠亞", "鬼头桃菜", "三上悠亚"]]


def test_search_actor_raises_not_found_when_no_exact_match(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "Somebody",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)

    with pytest.raises(MetadataNotFoundError) as exc_info:
        provider.search_actor("三上悠亚")

    assert exc_info.value.resource == "actor"
    assert exc_info.value.lookup_value == "三上悠亚"


def test_search_actors_returns_all_candidates_without_exact_match_filter(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "Somebody",
                                "avatar_url": "https://example.com/a1.jpg",
                            },
                            {
                                "id": "actor-2",
                                "type": 0,
                                "name": "Another",
                                "avatar_url": "https://example.com/a2.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)
    actors = provider.search_actors("三上悠亚")

    assert [actor.javdb_id for actor in actors] == ["actor-1", "actor-2"]
    assert [actor.javdb_type for actor in actors] == [0, 0]
    assert [actor.name for actor in actors] == ["Somebody", "Another"]


def test_search_actors_replaces_avatar_url_with_gfriends_when_available(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "avatar_url": "https://example.com/a1.jpg",
                            },
                            {
                                "id": "actor-2",
                                "type": 0,
                                "name": "Another",
                                "avatar_url": "https://example.com/a2.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )
    resolver = FakeActorImageResolver({"三上悠亚": "https://cdn.example.com/mikami.jpg"})

    provider = JavdbProvider(host=host, actor_image_resolver=resolver)
    actors = provider.search_actors("三上悠亚")

    assert [actor.avatar_url for actor in actors] == [
        "https://cdn.example.com/mikami.jpg",
        "https://example.com/a2.jpg",
    ]


def test_search_actors_falls_back_to_javdb_avatar_when_gfriends_missing(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host, actor_image_resolver=FakeActorImageResolver({}))
    actors = provider.search_actors("三上悠亚")

    assert actors[0].avatar_url == "https://example.com/actor.jpg"


def test_search_actors_falls_back_to_javdb_avatar_when_gfriends_errors(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "actors": [
                            {
                                "id": "actor-1",
                                "type": 0,
                                "name": "三上悠亚",
                                "avatar_url": "https://example.com/actor.jpg",
                            },
                        ]
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host, actor_image_resolver=FakeActorImageResolver(should_raise=True))
    actors = provider.search_actors("三上悠亚")

    assert actors[0].avatar_url == "https://example.com/actor.jpg"


def test_search_actors_raises_not_found_when_empty(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_search_url = build_url(
        host,
        "/api/v2/search?q=%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A&from_recent=false&type=actor&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", actor_search_url): FakeResponse({"success": 1, "data": {"actors": []}}),
        },
    )

    provider = JavdbProvider(host=host)

    with pytest.raises(MetadataNotFoundError) as exc_info:
        provider.search_actors("三上悠亚")

    assert exc_info.value.resource == "actor"
    assert exc_info.value.lookup_value == "三上悠亚"


def test_get_actor_movies_by_javdb_uses_actor_type_and_id(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    actor_movies_url = build_url(
        host,
        "/api/v1/movies/tags?filter_by=2:a:actor-7&sort_by=release&order_by=desc&page=3",
    )
    requests = install_request_stub(
        monkeypatch,
        {
            ("GET", actor_movies_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movies": [
                            {
                                "id": "aa11",
                                "number": "SSNI-001",
                                "title": "Newer",
                                "release_date": "2024-02-03",
                            }
                        ]
                    },
                }
            )
        },
    )

    provider = JavdbProvider(host=host)
    result = provider.get_actor_movies_by_javdb("actor-7", actor_type=2, page=3)

    assert [movie.javdb_id for movie in result] == ["aa11"]
    assert requests[0][1] == actor_movies_url


def test_get_movie_by_javdb_id_returns_mapped_detail(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    movie_detail_url = build_url(host, "/api/v4/movies/ab12cd?from_rankings=true")
    raw_payload = {
        "success": 1,
        "data": {
            "movie": {
                "id": "ab12cd",
                "title": "Matched",
                "number": "SSNI-888",
                "summary": "summary",
                "cover_url": "https://example.com/cover.jpg",
                "release_date": "2024-02-14",
                "duration": 150,
                "score": "4.6",
                "reviews_count": 200,
                "comments_count": 10,
                "want_watch_count": 20,
                "watched_count": 30,
                "series_name": "Series",
                "preview_video_url": "https://example.com/video.m3u8",
                "tags": [
                    {"id": "17", "name": "剧情"},
                    {"id": "18", "name": "企划"},
                ],
                "actors": [
                    {
                        "id": "ActorA1",
                        "name": "三上悠亚",
                        "avatar_url": "https://example.com/actor.jpg",
                    }
                ],
                "preview_images": [
                    {
                        "thumb_url": "https://example.com/p1_s.jpg",
                        "large_url": "https://example.com/p1_l.jpg",
                    }
                ],
            }
        },
    }
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_detail_url): FakeResponse(raw_payload)
        },
    )

    provider = JavdbProvider(host=host)
    detail = provider.get_movie_by_javdb_id("ab12cd")

    assert isinstance(detail, JavdbMovieDetailResource)
    assert detail.javdb_id == "ab12cd"
    assert detail.movie_number == "SSNI-888"
    assert detail.title == "Matched"
    assert detail.summary == "summary"
    assert detail.cover_image == "https://example.com/cover.jpg"
    assert str(detail.release_date) == "2024-02-14"
    assert detail.duration_minutes == 150
    assert detail.score == 4.6
    assert detail.score_number == 200
    assert detail.comment_count == 10
    assert detail.want_watch_count == 20
    assert detail.watched_count == 30
    assert detail.series_name == "Series"
    assert detail.is_subscribed is None
    assert detail.extra == raw_payload
    assert detail.tags[0].javdb_id == "17"
    assert detail.tags[0].name == "剧情"
    assert detail.actors[0].javdb_id == "ActorA1"
    assert detail.actors[0].name == "三上悠亚"
    assert detail.actors[0].avatar_url == "https://example.com/actor.jpg"


def test_get_movie_by_javdb_id_prefers_gfriends_actor_image(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    movie_detail_url = build_url(host, "/api/v4/movies/ab12cd?from_rankings=true")
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_detail_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movie": {
                            "id": "ab12cd",
                            "title": "Matched",
                            "number": "SSNI-888",
                            "summary": "summary",
                            "cover_url": "https://example.com/cover.jpg",
                            "duration": 150,
                            "tags": [],
                            "actors": [
                                {
                                    "id": "ActorA1",
                                    "name": "三上悠亚",
                                    "avatar_url": "https://example.com/actor.jpg",
                                }
                            ],
                            "preview_images": [],
                        }
                    },
                }
            )
        },
    )

    provider = JavdbProvider(
        host=host,
        actor_image_resolver=FakeActorImageResolver({"三上悠亚": "https://cdn.example.com/mikami.jpg"}),
    )
    detail = provider.get_movie_by_javdb_id("ab12cd")

    assert detail.actors[0].avatar_url == "https://cdn.example.com/mikami.jpg"


def test_get_movie_by_number_looks_up_movie_by_number_then_fetches_detail(
    monkeypatch: pytest.MonkeyPatch,
):
    host = "jdforrepam.com"
    movie_search_url = build_url(
        host,
        "/api/v2/search?q=SSNI-888&from_recent=false&type=movie&movie_type=all&movie_sort_by=relevance&movie_filter_by=all&page=1&limit=24",
    )
    movie_detail_url = build_url(host, "/api/v4/movies/ab12cd?from_rankings=true")
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movies": [
                            {
                                "id": "xx00",
                                "number": "SSNI-889",
                                "title": "Other",
                            },
                            {
                                "id": "ab12cd",
                                "number": "SSNI-888",
                                "title": "Matched",
                            },
                        ]
                    },
                }
            ),
            ("GET", movie_detail_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movie": {
                            "id": "ab12cd",
                            "title": "Matched",
                            "number": "SSNI-888",
                            "summary": "summary",
                            "cover_url": "https://example.com/cover.jpg",
                            "release_date": "2024-02-14",
                            "duration": 150,
                            "score": "4.6",
                            "reviews_count": 200,
                            "comments_count": 10,
                            "want_watch_count": 20,
                            "watched_count": 30,
                            "tags": [],
                            "actors": [],
                            "preview_images": [],
                        }
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)
    detail = provider.get_movie_by_number("SSNI-888")

    assert detail.javdb_id == "ab12cd"
    assert detail.movie_number == "SSNI-888"


def test_get_movie_by_number_normalizes_search_number(monkeypatch: pytest.MonkeyPatch):
    host = "jdforrepam.com"
    movie_search_url = build_url(
        host,
        "/api/v2/search?q=SSNI-888&from_recent=false&type=movie&movie_type=all&movie_sort_by=relevance&movie_filter_by=all&page=1&limit=24",
    )
    movie_detail_url = build_url(host, "/api/v4/movies/ab12cd?from_rankings=true")
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_search_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movies": [
                            {
                                "id": "ab12cd",
                                "number": "SSNI-888",
                                "title": "Matched",
                            }
                        ]
                    },
                }
            ),
            ("GET", movie_detail_url): FakeResponse(
                {
                    "success": 1,
                    "data": {
                        "movie": {
                            "id": "ab12cd",
                            "title": "Matched",
                            "number": "SSNI-888",
                            "summary": "",
                            "cover_url": "https://example.com/cover.jpg",
                            "duration": 150,
                            "tags": [],
                            "actors": [],
                            "preview_images": [],
                        }
                    },
                }
            ),
        },
    )

    provider = JavdbProvider(host=host)
    detail = provider.get_movie_by_number(" ppv-ssni-888 ")

    assert detail.javdb_id == "ab12cd"


def test_get_movie_by_number_raises_not_found_for_missing_number(
    monkeypatch: pytest.MonkeyPatch,
):
    host = "jdforrepam.com"
    movie_search_url = build_url(
        host,
        "/api/v2/search?q=SSNI-404&from_recent=false&type=movie&movie_type=all&movie_sort_by=relevance&movie_filter_by=all&page=1&limit=24",
    )
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_search_url): FakeResponse(
                {"success": 1, "data": {"movies": []}}
            )
        },
    )

    provider = JavdbProvider(host=host)

    with pytest.raises(MetadataNotFoundError) as exc_info:
        provider.get_movie_by_number("SSNI-404")

    assert exc_info.value.resource == "movie"
    assert exc_info.value.lookup_value == "SSNI-404"


def test_get_movie_by_javdb_id_raises_request_error_for_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    host = "jdforrepam.com"
    movie_detail_url = build_url(host, "/api/v4/movies/ab12cd?from_rankings=true")
    install_request_stub(
        monkeypatch,
        {
            ("GET", movie_detail_url): FakeResponse({"success": 0, "message": "bad request"})
        },
    )

    provider = JavdbProvider(host=host)

    with pytest.raises(MetadataRequestError):
        provider.get_movie_by_javdb_id("ab12cd")
