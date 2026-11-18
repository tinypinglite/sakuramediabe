from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.exception.errors import ApiError
from src.service.transfers.imports import import_service


@pytest.mark.parametrize("supported, with_callback", [(False, True), (True, True), (True, False)])
def test_import_scan_passes_progress_only_to_supported_plugins(monkeypatch, supported, with_callback):
    library = SimpleNamespace(provider_key="cloud115")
    monkeypatch.setattr(import_service.MediaLibrary, "get_or_none", lambda *_: library)
    monkeypatch.setattr(import_service.MEDIA_PROVIDER_REGISTRY, "supports_scan_progress", lambda _: supported)
    callback = Mock() if with_callback else None
    payload = {"current": 1, "total": 2, "text": "解析目录路径"}

    def scan(**kwargs):
        if "progress_callback" in kwargs:
            kwargs["progress_callback"](payload)
        # 在扫描边界结束测试，不进入数据库导入流程。
        raise RuntimeError("stop after scan")

    storage = SimpleNamespace(scan_import_source=Mock(side_effect=scan))
    service = object.__new__(import_service.MediaImportService)
    service._provider_override = storage
    source_ref = {"kind": "directory"}
    with pytest.raises(ApiError):
        service.import_from_source(source_ref, 1, progress_callback=callback)
    expected = {"source_ref": source_ref}
    if supported and with_callback:
        expected["progress_callback"] = callback
        callback.assert_called_once_with(payload)
    elif callback is not None:
        callback.assert_not_called()
    storage.scan_import_source.assert_called_once_with(**expected)
