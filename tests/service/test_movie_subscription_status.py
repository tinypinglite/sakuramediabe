"""订阅影片状态判定（``MovieSubscriptionService._status_expression``）。

该 CASE 是筛选 / 计数 / 列表展示三处的唯一定义，此前没有直接测试。这里锁住两条它必须
一直成立的性质：

1. **七态互斥且完备**：各状态计数之和恒等于订阅总数；
2. **``downloading`` ∪ ``import_failed`` == 旧的"有活跃下载任务"**——细分只是把一个桶
   一分为二，不能让任何影片漂到别的桶去。第 2 条尤其重要：搜索闸门
   （``subscribed_movie_auto_download_service`` 里的 ``~active_download_task_exists_expression``）
   读的是同一个"活跃任务"集合，两边一旦脱节就会出现"页面说缺资源、调度说别搜"。
"""

import pytest

from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.model import (
    DownloadClient,
    DownloadTask,
    Media,
    MediaLibrary,
    Movie,
)
from src.schema.catalog.subscriptions import MovieSubscriptionStatus
from src.service.catalog import MovieSubscriptionService


@pytest.fixture()
def client(test_db):
    library = MediaLibrary.create(name="library", provider_key="test", provider_config={})
    return DownloadClient.create(
        name="client",
        library=library,
        provider_config={},
    )


def _subscribe(movie_number: str) -> Movie:
    # javdb_id 有唯一约束且默认空串，同一用例建多部片时必须各给一个值，否则第二条就撞键。
    return Movie.create(
        movie_number=movie_number,
        javdb_id=f"javdb-{movie_number}",
        title=f"title {movie_number}",
        is_subscribed=True,
    )


def _task(
    client, movie_number: str, *, state: str, import_status: str, seq: int = 0
):
    # (client, remote_id) 有唯一约束，同一部片挂多条任务时必须给不同的 seq。
    return DownloadTask.create(
        client=client,
        movie=movie_number,
        name=f"{movie_number}.mkv",
        remote_id=f"remote-{movie_number}-{seq}",
        progress=1.0,
        state=state,
        completed_source_ref=(
            {"source": movie_number} if state == "completed" else None
        ),
        import_status=import_status,
    )


def _status_of(movie_number: str) -> str:
    page = MovieSubscriptionService.list_subscriptions(page=1, page_size=50)
    by_number = {item.movie_number: item.status for item in page.items}
    return by_number[movie_number]


@pytest.mark.parametrize(
    "import_status",
    [
        IMPORT_STATUS_FAILED,
        # 导入跑完却零产出：整包只有小于阈值的样本文件时，扫描记 skipped、failed_count 为 0，
        # 任务落 completed 但一个 Media 都没有。按"导入报没报失败"切会把这类漏在下载中里。
        IMPORT_STATUS_COMPLETED,
    ],
)
def test_finished_import_without_media_is_import_failed(client, import_status):
    """下完了但库里没有 —— 这类片过去和真下载中混在一个桶里，用户看到"下载中"却在
    下载器里找不到进度，正是本次细分要解决的问题。"""
    _subscribe("ABP-001")
    _task(
        client,
        "ABP-001",
        state="completed",
        import_status=import_status,
    )

    assert _status_of("ABP-001") == MovieSubscriptionStatus.IMPORT_FAILED.value


@pytest.mark.parametrize(
    "state,import_status",
    [
        ("downloading", IMPORT_STATUS_PENDING),
        ("queued", IMPORT_STATUS_PENDING),
        # 下完了正等自动导入：秒级过渡态，仍归下载中——用户对它的动作和真下载中一样。
        ("completed", IMPORT_STATUS_PENDING),
        # 导入作业正在跑：同样还在路上。
        ("completed", IMPORT_STATUS_RUNNING),
    ],
)
def test_active_tasks_with_unfinished_import_stay_downloading(
    client, state, import_status
):
    _subscribe("ABP-002")
    _task(
        client,
        "ABP-002",
        state=state,
        import_status=import_status,
    )

    assert _status_of("ABP-002") == MovieSubscriptionStatus.DOWNLOADING.value


def test_one_unfinished_task_keeps_movie_downloading(client):
    """一部片同时挂着"导入失败"和"还在下"两条任务时算下载中——还有希望，不该报导入失败。"""
    _subscribe("MIX-001")
    _task(
        client,
        "MIX-001",
        state="completed",
        import_status=IMPORT_STATUS_FAILED,
        seq=0,
    )
    _task(
        client,
        "MIX-001",
        state="downloading",
        import_status=IMPORT_STATUS_PENDING,
        seq=1,
    )

    assert _status_of("MIX-001") == MovieSubscriptionStatus.DOWNLOADING.value


def test_media_wins_over_import_failed(client):
    """已入库优先级最高：同一部片可能既有成功导入的媒体，又留着一条导入失败的旧任务。"""
    _subscribe("ABP-003")
    _task(
        client,
        "ABP-003",
        state="completed",
        import_status=IMPORT_STATUS_FAILED,
    )
    Media.create(movie="ABP-003", library=client.library, file_name="ABP-003.mkv")

    assert _status_of("ABP-003") == MovieSubscriptionStatus.IMPORTED.value


@pytest.mark.parametrize("dead_state", ["failed"])
def test_dead_tasks_never_count_as_import_failed(client, dead_state):
    """判死的任务不算活跃，即便它的 import_status 也是 failed——否则死种会把影片
    永久钉在 import_failed，再也不去找别的种子。"""
    _subscribe("ABP-004")
    _task(
        client,
        "ABP-004",
        state=dead_state,
        import_status=IMPORT_STATUS_FAILED,
    )

    assert _status_of("ABP-004") == MovieSubscriptionStatus.PENDING.value


def test_status_counts_sum_equals_total(client):
    """七态完备性：各状态计数之和恒等于订阅总数。"""
    _subscribe("ABP-101")
    _task(
        client,
        "ABP-101",
        state="completed",
        import_status=IMPORT_STATUS_FAILED,
    )
    _subscribe("ABP-102")
    _task(
        client,
        "ABP-102",
        state="downloading",
        import_status=IMPORT_STATUS_PENDING,
    )
    _subscribe("ABP-103")
    _task(
        client,
        "ABP-103",
        state="completed",
        import_status=IMPORT_STATUS_COMPLETED,
    )
    Media.create(movie="ABP-103", library=client.library, file_name="ABP-103.mkv")
    _subscribe("ABP-104")  # 从未查过 -> pending

    counts = MovieSubscriptionService.count_by_status()

    assert counts.import_failed == 1
    assert counts.downloading == 1
    assert counts.imported == 1
    assert counts.pending == 1
    per_status_sum = (
        counts.imported
        + counts.import_failed
        + counts.downloading
        + counts.pending
        + counts.missing
        + counts.exhausted
        + counts.failed
    )
    assert per_status_sum == counts.total == 4


def test_import_failed_and_downloading_partition_the_active_set(client):
    """细分不改变归属集合：两个新桶的并集恰好是原来"有活跃下载任务"的那批。"""
    expected_active = set()
    for index, (state, import_status) in enumerate(
        [
            ("completed", IMPORT_STATUS_FAILED),
            ("downloading", IMPORT_STATUS_PENDING),
            ("completed", IMPORT_STATUS_PENDING),
            ("queued", IMPORT_STATUS_FAILED),
            # 导入跑完零产出：归 import_failed，但仍必须留在活跃集合内，不能漂去"缺资源"。
            ("completed", IMPORT_STATUS_COMPLETED),
        ]
    ):
        number = f"ACT-{index:03d}"
        _subscribe(number)
        _task(
            client,
            number,
            state=state,
            import_status=import_status,
        )
        expected_active.add(number)

    # 失败任务与压根没任务的都不该进这两个桶。
    _subscribe("DEAD-001")
    _task(
        client,
        "DEAD-001",
        state="failed",
        import_status=IMPORT_STATUS_FAILED,
    )
    _subscribe("NONE-001")

    page = MovieSubscriptionService.list_subscriptions(page=1, page_size=50)
    actual_active = {
        item.movie_number
        for item in page.items
        if item.status
        in (
            MovieSubscriptionStatus.IMPORT_FAILED.value,
            MovieSubscriptionStatus.DOWNLOADING.value,
        )
    }

    assert actual_active == expected_active


def test_status_filter_selects_only_import_failed(client):
    _subscribe("FIL-001")
    _task(
        client,
        "FIL-001",
        state="completed",
        import_status=IMPORT_STATUS_FAILED,
    )
    _subscribe("FIL-002")
    _task(
        client,
        "FIL-002",
        state="downloading",
        import_status=IMPORT_STATUS_PENDING,
    )

    page = MovieSubscriptionService.list_subscriptions(
        page=1,
        page_size=50,
        status=MovieSubscriptionStatus.IMPORT_FAILED,
    )

    assert [item.movie_number for item in page.items] == ["FIL-001"]
    assert page.total == 1
    assert page.items[0].movie_id == Movie.get(Movie.movie_number == "FIL-001").id


def test_search_state_branches_restore_subscription_statuses(client):
    from src.common.runtime_time import utc_now_for_db
    from src.service.catalog.movie_subscription_search_state_service import (
        ERROR_CODE_NO_CANDIDATE,
        STATE_EXHAUSTED,
        STATE_FAILED_RETRYABLE,
        STATE_SUCCEEDED,
    )

    now = utc_now_for_db()
    exhausted = _subscribe("SRCH-001")
    no_candidate = _subscribe("SRCH-002")
    infra_failed = _subscribe("SRCH-003")
    succeeded = _subscribe("SRCH-004")
    Movie.update(
        subscription_search_state=STATE_EXHAUSTED,
        subscription_search_attempt_count=3,
        subscription_search_last_attempted_at=now,
    ).where(Movie.id == exhausted.id).execute()
    Movie.update(
        subscription_search_state=STATE_FAILED_RETRYABLE,
        subscription_search_attempt_count=1,
        subscription_search_error_code=ERROR_CODE_NO_CANDIDATE,
        subscription_search_last_attempted_at=now,
    ).where(Movie.id == no_candidate.id).execute()
    Movie.update(
        subscription_search_state=STATE_FAILED_RETRYABLE,
        subscription_search_error_code="indexer_search_failed",
        subscription_search_last_error="indexer timeout",
        subscription_search_last_attempted_at=now,
    ).where(Movie.id == infra_failed.id).execute()
    Movie.update(
        subscription_search_state=STATE_SUCCEEDED,
        subscription_search_last_attempted_at=now,
    ).where(Movie.id == succeeded.id).execute()

    assert _status_of("SRCH-001") == MovieSubscriptionStatus.EXHAUSTED.value
    assert _status_of("SRCH-002") == MovieSubscriptionStatus.MISSING.value
    assert _status_of("SRCH-003") == MovieSubscriptionStatus.FAILED.value
    assert _status_of("SRCH-004") == MovieSubscriptionStatus.MISSING.value

    page = MovieSubscriptionService.list_subscriptions(page=1, page_size=20)
    item = next(item for item in page.items if item.movie_number == "SRCH-003")
    assert item.last_error == "indexer timeout"
