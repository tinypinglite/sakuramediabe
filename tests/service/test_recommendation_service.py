from src.api.exception.errors import ApiError
from src.model import Actor, Image, Media, Movie, MovieActor, MovieSimilarity, MovieTag, Tag
from src.service.discovery.recommendation_service import (
    HEAT_BOOST_ALPHA,
    SIM_WEIGHT_ACTOR,
    SIM_WEIGHT_TAG,
    MovieRecommendationService,
)


def _create_movie(movie_number: str, javdb_id: str, **kwargs) -> Movie:
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _attach_actors(movie: Movie, actors) -> None:
    for actor in actors:
        MovieActor.create(movie=movie, actor=actor)


def _attach_tags(movie: Movie, tags) -> None:
    for tag in tags:
        MovieTag.create(movie=movie, tag=tag)


def test_compute_for_movie_returns_empty_when_source_has_no_signals(app):
    source = _create_movie("ABP-001", "MovieA1")
    target = _create_movie("ABP-002", "MovieA2")
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    _attach_actors(target, [actor])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0)

    assert ranked == []


def test_compute_for_movie_excludes_collection_targets(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    candidate = _create_movie("ABP-002", "MovieA2")
    collection_target = _create_movie("ABP-003", "MovieA3", is_collection=True)
    _attach_actors(source, [actor])
    _attach_actors(candidate, [actor])
    _attach_actors(collection_target, [actor])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0)

    target_ids = [target_id for target_id, _ in ranked]
    assert candidate.id in target_ids
    assert collection_target.id not in target_ids


def test_compute_for_movie_score_matches_weighted_jaccard_with_heat_boost(app):
    actor_a = Actor.create(name="ActorA", javdb_id="actor-a")
    actor_b = Actor.create(name="ActorB", javdb_id="actor-b")
    tag_x = Tag.create(name="TagX")
    tag_y = Tag.create(name="TagY")

    source = _create_movie("ABP-001", "MovieA1")
    candidate = _create_movie("ABP-002", "MovieA2", heat=50)
    _attach_actors(source, [actor_a, actor_b])
    _attach_tags(source, [tag_x])
    _attach_actors(candidate, [actor_a])
    _attach_tags(candidate, [tag_x, tag_y])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0)

    assert len(ranked) == 1
    target_id, score = ranked[0]
    assert target_id == candidate.id
    expected_actor_jaccard = 1 / 2  # 共同 ActorA / 并集 {ActorA, ActorB}
    expected_tag_jaccard = 1 / 2  # 共同 TagX / 并集 {TagX, TagY}
    expected_base = SIM_WEIGHT_ACTOR * expected_actor_jaccard + SIM_WEIGHT_TAG * expected_tag_jaccard
    expected_boost = 1 + HEAT_BOOST_ALPHA * (50 / 100)
    assert score == expected_base * expected_boost


def test_compute_for_movie_orders_by_final_score_desc_and_heat_breaks_ties(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    candidate_high_heat = _create_movie("ABP-002", "MovieA2", heat=100)
    candidate_low_heat = _create_movie("ABP-003", "MovieA3", heat=0)
    _attach_actors(source, [actor])
    _attach_actors(candidate_high_heat, [actor])
    _attach_actors(candidate_low_heat, [actor])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0)

    assert [target_id for target_id, _ in ranked] == [
        candidate_high_heat.id,
        candidate_low_heat.id,
    ]


def test_heat_boost_does_not_override_relevance_signal(app):
    actor_main = Actor.create(name="ActorMain", javdb_id="actor-main")
    actor_other = Actor.create(name="ActorOther", javdb_id="actor-other")
    tag_a = Tag.create(name="TagA")
    tag_b = Tag.create(name="TagB")
    tag_c = Tag.create(name="TagC")

    source = _create_movie("ABP-001", "MovieA1")
    _attach_actors(source, [actor_main])
    _attach_tags(source, [tag_a, tag_b, tag_c])

    # 强相关候选：演员/标签都对齐，热度为 0
    strong_candidate = _create_movie("ABP-002", "MovieA2", heat=0)
    _attach_actors(strong_candidate, [actor_main])
    _attach_tags(strong_candidate, [tag_a, tag_b, tag_c])

    # 弱相关爆款：仅一个标签匹配，热度极高
    hot_candidate = _create_movie("ABP-003", "MovieA3", heat=100000)
    _attach_actors(hot_candidate, [actor_other])
    _attach_tags(hot_candidate, [tag_a])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0)

    target_ids = [target_id for target_id, _ in ranked]
    assert target_ids[0] == strong_candidate.id


def test_compute_for_movie_truncates_to_top_n(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    _attach_actors(source, [actor])
    for index in range(5):
        candidate = _create_movie(f"ABP-1{index:02d}", f"MovieA1{index:02d}")
        _attach_actors(candidate, [actor])

    service = MovieRecommendationService()
    ranked = service.compute_for_movie(source.id, heat_ref=100.0, top_n=3)

    assert len(ranked) == 3


def test_recompute_all_persists_top_n_and_skips_collection_sources(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    target = _create_movie("ABP-002", "MovieA2", heat=10)
    collection_source = _create_movie("ABP-COL", "Collection", is_collection=True)
    _attach_actors(source, [actor])
    _attach_actors(target, [actor])
    _attach_actors(collection_source, [actor])

    service = MovieRecommendationService()
    stats = service.recompute_all(top_n=10)

    assert stats["total_movies"] == 2  # 合集影片不在 source 列表里
    assert stats["processed_movies"] == 2
    assert stats["stored_pairs"] >= 1

    # 合集影片不应作为 source 出现
    similarity_sources = {
        row.source_movie_id
        for row in MovieSimilarity.select(MovieSimilarity.source_movie)
    }
    assert collection_source.id not in similarity_sources

    # source 的 Top-N 中应包含 target，且 rank=1
    rows = list(
        MovieSimilarity.select()
        .where(MovieSimilarity.source_movie == source.id)
        .order_by(MovieSimilarity.rank.asc())
    )
    assert rows[0].target_movie_id == target.id
    assert rows[0].rank == 1


def test_recompute_all_purges_stale_rows_for_collection_sources(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    target = _create_movie("ABP-002", "MovieA2")
    _attach_actors(source, [actor])
    _attach_actors(target, [actor])

    service = MovieRecommendationService()
    service.recompute_all(top_n=10)

    source.is_collection = True
    source.save()
    stats = service.recompute_all(top_n=10)

    assert stats["total_movies"] == 1
    assert (
        MovieSimilarity.select()
        .where(MovieSimilarity.source_movie == source.id)
        .count()
        == 0
    )


def test_replace_similarity_rows_atomic_replaces_existing(app):
    actor = Actor.create(name="Actor1", javdb_id="actor-1")
    source = _create_movie("ABP-001", "MovieA1")
    target_old = _create_movie("ABP-002", "MovieA2")
    target_new = _create_movie("ABP-003", "MovieA3")
    _attach_actors(source, [actor])
    _attach_actors(target_old, [actor])
    _attach_actors(target_new, [actor])

    service = MovieRecommendationService()
    service.replace_similarity_rows(source.id, [(target_old.id, 0.9)])
    assert MovieSimilarity.select().count() == 1

    service.replace_similarity_rows(source.id, [(target_new.id, 0.8)])
    rows = list(
        MovieSimilarity.select().where(MovieSimilarity.source_movie == source.id)
    )
    assert len(rows) == 1
    assert rows[0].target_movie_id == target_new.id
    assert rows[0].rank == 1


def test_list_similar_returns_in_rank_order(app):
    source = _create_movie("ABP-001", "MovieA1")
    target_a = _create_movie("ABP-002", "MovieA2")
    target_b = _create_movie("ABP-003", "MovieA3")
    MovieSimilarity.create(source_movie=source, target_movie=target_b, score=0.5, rank=2)
    MovieSimilarity.create(source_movie=source, target_movie=target_a, score=0.8, rank=1)

    service = MovieRecommendationService()
    items = service.list_similar(movie_number="ABP-001", limit=10)

    assert [item.movie.movie_number for item in items] == ["ABP-002", "ABP-003"]
    assert items[0].similarity_score == 0.8


def test_list_similar_returns_empty_when_no_records(app):
    _create_movie("ABP-001", "MovieA1")

    service = MovieRecommendationService()
    items = service.list_similar(movie_number="ABP-001", limit=10)

    assert items == []


def test_list_similar_resources_supports_normalized_lookup_and_movie_flags(app):
    source = _create_movie("FC2-PPV-123456", "MovieA1")
    thin_cover_image = Image.create(
        origin="similar/thin-origin.jpg",
        small="similar/thin-small.jpg",
        medium="similar/thin-medium.jpg",
        large="similar/thin-large.jpg",
    )
    target = _create_movie(
        "FC2-PPV-654321",
        "MovieA2",
        title_zh="相似中文标题",
        thin_cover_image=thin_cover_image,
    )
    Media.create(
        movie=target,
        path="/library/main/fc2-ppv-654321.mp4",
        valid=True,
        special_tags="4K 无码",
    )
    MovieSimilarity.create(source_movie=source, target_movie=target, score=0.88, rank=1)

    service = MovieRecommendationService()
    items = service.list_similar_resources(movie_number="fc2-123456", limit=10)

    assert len(items) == 1
    assert items[0].movie_number == "FC2-PPV-654321"
    assert items[0].title_zh == "相似中文标题"
    assert items[0].thin_cover_image is not None
    assert items[0].thin_cover_image.id == thin_cover_image.id
    assert items[0].can_play is True
    assert items[0].is_4k is True
    assert items[0].similarity_score == 0.88


def test_list_similar_raises_when_movie_not_found(app):
    service = MovieRecommendationService()

    raised: ApiError | None = None
    try:
        service.list_similar(movie_number="UNKNOWN-001", limit=10)
    except ApiError as exc:
        raised = exc
    assert raised is not None
    assert raised.status_code == 404
    assert raised.code == "movie_not_found"
