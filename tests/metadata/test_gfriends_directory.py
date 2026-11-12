import gzip
import json

from src.metadata.gfriends import GfriendsActorImageResolver


def test_bundled_filetree_hydrates_and_searches_without_network(tmp_path):
    bundled_path = tmp_path / "gfriends-filetree.json.gz"
    with gzip.open(bundled_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {"Content": {"三上悠亚.jpg": "三上悠亚/三上悠亚.jpg"}},
            handle,
            ensure_ascii=False,
        )
    directory = GfriendsActorImageResolver(
        filetree_url="https://example.invalid/Filetree.json",
        cdn_base_url="https://cdn.example.invalid/gfriends",
        cache_path=str(tmp_path / "missing.json"),
        cache_ttl_hours=24,
        bundled_filetree_path=bundled_path,
    )

    candidates = directory.search("三上", limit=5)

    assert candidates
    assert candidates[0].name
    assert candidates[0].candidate_id
    assert candidates[0].avatar_url.startswith(
        "https://cdn.example.invalid/gfriends/"
    )
    assert directory.candidate(candidates[0].candidate_id) == candidates[0]


def test_filetree_searches_and_resolves_han_script_variants(tmp_path):
    bundled_path = tmp_path / "gfriends-filetree.json.gz"
    with gzip.open(bundled_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {"Content": {"亜美.jpg": "亜美/亜美.jpg"}},
            handle,
            ensure_ascii=False,
        )
    directory = GfriendsActorImageResolver(
        filetree_url="https://example.invalid/Filetree.json",
        cdn_base_url="https://cdn.example.invalid/gfriends",
        cache_path=str(tmp_path / "missing.json"),
        cache_ttl_hours=24,
        bundled_filetree_path=bundled_path,
    )

    for query in ("亚美", "亜美", "亞美"):
        candidates = directory.search(query)
        assert [candidate.name for candidate in candidates] == ["亜美"]
    assert directory.resolve(["亚美"]).endswith("/亜美/亜美.jpg")


def test_filetree_search_prefers_original_form_for_equal_matches(tmp_path):
    bundled_path = tmp_path / "gfriends-filetree.json.gz"
    with gzip.open(bundled_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "Content": {
                    "亚美.jpg": "characters/亚美/亚美.jpg",
                    "亜美.jpg": "characters/亜美/亜美.jpg",
                }
            },
            handle,
            ensure_ascii=False,
        )
    directory = GfriendsActorImageResolver(
        filetree_url="https://example.invalid/Filetree.json",
        cdn_base_url="https://cdn.example.invalid/gfriends",
        cache_path=str(tmp_path / "missing.json"),
        cache_ttl_hours=24,
        bundled_filetree_path=bundled_path,
    )

    assert [candidate.name for candidate in directory.search("亚美")] == [
        "亚美",
        "亜美",
    ]
