from types import SimpleNamespace

from src.scheduler.queue_tasks import (
    LANE_CONCURRENCY,
    LANE_TRANSFER,
    NON_DEFAULT_LANE_TASK_KEYS,
    QUEUE_TASK_REGISTRY,
    lane_task_keys,
)
from src.scheduler.worker import TaskWorker
from src.service.transfers.shared.media_transfer_task_service import (
    MediaTransferTaskService,
)


def test_media_transfer_uses_dedicated_single_worker_lane():
    assert LANE_CONCURRENCY[LANE_TRANSFER] == 1
    assert lane_task_keys(LANE_TRANSFER) == {MediaTransferTaskService.TASK_KEY}
    assert MediaTransferTaskService.TASK_KEY in NON_DEFAULT_LANE_TASK_KEYS
    assert QUEUE_TASK_REGISTRY[MediaTransferTaskService.TASK_KEY].business_recovery is not None


def test_worker_runs_business_recovery_immediately_after_task_failure(monkeypatch):
    recovered: list[set[str]] = []

    def fail_task(**_kwargs):
        raise RuntimeError("transfer_failed")

    monkeypatch.setattr("src.scheduler.worker.ActivityService.run_task", fail_task)
    worker = TaskWorker()
    monkeypatch.setattr(
        worker,
        "_run_business_recovery",
        lambda task_keys: recovered.append(task_keys),
    )

    worker._execute(
        SimpleNamespace(
            id=123,
            task_key=MediaTransferTaskService.TASK_KEY,
            params={"media_ids": [1], "target_library_id": 2},
        )
    )

    assert recovered == [{MediaTransferTaskService.TASK_KEY}]


def test_worker_start_reconciles_interrupted_transfer_state_before_claiming(monkeypatch):
    recovered: list[set[str]] = []
    started_threads: list[str] = []

    class FakeThread:
        def __init__(self, *, name, **_kwargs):
            self.name = name

        def start(self):
            started_threads.append(self.name)

    monkeypatch.setattr("src.scheduler.worker.threading.Thread", FakeThread)
    worker = TaskWorker(lanes={LANE_TRANSFER: 0})
    monkeypatch.setattr(
        worker,
        "_run_business_recovery",
        lambda task_keys: recovered.append(task_keys),
    )

    worker.start()

    assert MediaTransferTaskService.TASK_KEY in recovered[0]
    assert started_threads == ["task-worker-housekeeper"]
