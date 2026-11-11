from src.metadata.gfriends import GfriendsActorImageResolver


def test_bundled_filetree_hydrates_and_searches_without_network(tmp_path):
    directory = GfriendsActorImageResolver(
        filetree_url="https://example.invalid/Filetree.json",
        cdn_base_url="https://cdn.example.invalid/gfriends",
        cache_path=str(tmp_path / "missing.json"),
        cache_ttl_hours=24,
    )

    candidates = directory.search("三上", limit=5)

    assert candidates
    assert candidates[0].name
    assert candidates[0].candidate_id
    assert candidates[0].avatar_url.startswith(
        "https://cdn.example.invalid/gfriends/"
    )
    assert directory.candidate(candidates[0].candidate_id) == candidates[0]
