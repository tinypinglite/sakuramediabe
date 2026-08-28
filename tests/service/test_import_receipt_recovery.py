from types import SimpleNamespace

from src.service.transfers.shared import import_task_service
from src.service.transfers.shared.import_task_service import ImportTaskService


def test_recover_staged_receipts_finalizes_committed_and_aborts_uncommitted(monkeypatch):
    class Storage:
        def __init__(self):
            self.finalized = []
            self.aborted = []

        def finalize_import(self, *, receipt):
            self.finalized.append(receipt)

        def abort_import(self, *, receipt):
            self.aborted.append(receipt)

    storage = Storage()
    library = SimpleNamespace(id=7, provider_key="demo")
    task_run = SimpleNamespace(
        id=42,
        params={
            "media_kind": "video",
            "library_id": 7,
            "source_ref": {"source": "opaque"},
            "source_disposition": "keep",
            "_staged_receipts": {
                "committed-op": {"receipt": {"receipt": "committed"}, "committed": True},
                "pending-op": {"receipt": {"receipt": "pending"}, "committed": False},
            },
        },
    )
    cleared = []
    monkeypatch.setattr(import_task_service.MediaLibrary, "get_by_id", lambda _id: library)
    monkeypatch.setattr(import_task_service, "library_handle_for", lambda _library: object())
    monkeypatch.setattr(import_task_service.MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: storage)
    monkeypatch.setattr(
        ImportTaskService,
        "_clear_stage_receipt",
        lambda task_run_id, operation_key: cleared.append((task_run_id, operation_key)),
    )

    assert ImportTaskService._recover_staged_receipts(task_run) is True
    assert storage.finalized == [{"receipt": "committed"}]
    assert storage.aborted == [{"receipt": "pending"}]
    assert cleared == [(42, "committed-op"), (42, "pending-op")]
