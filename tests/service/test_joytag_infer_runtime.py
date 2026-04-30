from types import SimpleNamespace
import sys

import numpy as np
import pytest

from joytag_infer import runtime as runtime_module
from joytag_infer.runtime import JoyTagOnnxRuntime
from joytag_infer.settings import JoyTagInferSettings


class _FakeOrtSession:
    last_instance = None
    default_execution_provider = "OpenVINOExecutionProvider"
    default_fail_on_run = False

    def __init__(self, _model_path: str, *, providers):
        self.providers = providers
        self.disable_fallback_called = False
        self.execution_provider = self.default_execution_provider
        self.run_calls = 0
        self.fail_on_run = self.default_fail_on_run
        _FakeOrtSession.last_instance = self

    def disable_fallback(self) -> None:
        self.disable_fallback_called = True

    def get_inputs(self):
        return [SimpleNamespace(name="input")]

    def get_outputs(self):
        return [SimpleNamespace(name="output", shape=[1, 768])]

    def get_providers(self):
        return [self.execution_provider]

    def run(self, _output_names, inputs):
        self.run_calls += 1
        if self.fail_on_run:
            raise RuntimeError("probe failed")
        batch = next(iter(inputs.values()))
        batch_size = int(batch.shape[0])
        return [np.ones((batch_size, 768), dtype=np.float32)]


class _FakeOrtModule:
    InferenceSession = _FakeOrtSession
    available_providers = ["OpenVINOExecutionProvider", "CPUExecutionProvider"]

    @staticmethod
    def get_available_providers():
        return list(_FakeOrtModule.available_providers)


class _FakeOpenVinoCore:
    available_devices = ["CPU"]

    def get_property(self, device_name: str, property_name: str):
        assert property_name == "FULL_DEVICE_NAME"
        return f"Fake {device_name}"


def _create_model_file(tmp_path) -> str:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"fake-model")
    return str(model_path)


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    _FakeOrtSession.last_instance = None
    _FakeOrtSession.default_execution_provider = "OpenVINOExecutionProvider"
    _FakeOrtSession.default_fail_on_run = False
    _FakeOrtModule.available_providers = ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.delitem(sys.modules, "pynvml", raising=False)


def test_openvino_runtime_rejects_missing_gpu_device(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    _FakeOpenVinoCore.available_devices = ["CPU"]

    with pytest.raises(RuntimeError, match="Requested OpenVINO device GPU is unavailable"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="openvino",
                model_path=_create_model_file(tmp_path),
                openvino_device_type="GPU",
            )
        )


def test_openvino_runtime_validates_gpu_at_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    _FakeOpenVinoCore.available_devices = ["CPU", "GPU.0"]

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="GPU",
        )
    )

    assert runtime.device == "Fake GPU.0"
    assert runtime.device_full_name == "Fake GPU.0"
    assert _FakeOrtSession.last_instance.disable_fallback_called is True
    assert _FakeOrtSession.last_instance.run_calls == 1
    assert _FakeOrtSession.last_instance.providers == [
        ("OpenVINOExecutionProvider", {"device_type": "GPU"})
    ]
    assert runtime.runtime_info(probe=False)["device"] == "Fake GPU.0"


def test_openvino_runtime_raises_when_gpu_probe_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    _FakeOpenVinoCore.available_devices = ["GPU.0"]

    original_init = _FakeOrtSession.__init__

    def _init_with_probe_failure(self, model_path: str, *, providers):
        original_init(self, model_path, providers=providers)
        self.fail_on_run = True

    monkeypatch.setattr(_FakeOrtSession, "__init__", _init_with_probe_failure)

    with pytest.raises(RuntimeError, match="OpenVINO GPU validation probe failed"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="openvino",
                model_path=_create_model_file(tmp_path),
                openvino_device_type="GPU",
            )
        )


def test_openvino_runtime_cpu_mode_uses_visible_cpu_without_gpu_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    _FakeOpenVinoCore.available_devices = ["CPU"]

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="CPU",
        )
    )

    assert runtime.device == "cpu"
    assert runtime.device_full_name == "Fake CPU"
    assert _FakeOrtSession.last_instance.disable_fallback_called is False
    assert _FakeOrtSession.last_instance.run_calls == 0


def test_cuda_runtime_validates_provider_and_reports_gpu_name(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CUDAExecutionProvider"

    class _FakeNvml:
        initialized = False

        @classmethod
        def nvmlInit(cls):
            cls.initialized = True

        @staticmethod
        def nvmlDeviceGetHandleByIndex(index: int):
            assert index == 0
            return object()

        @staticmethod
        def nvmlDeviceGetName(_handle):
            return b"NVIDIA GeForce RTX 3060"

        @classmethod
        def nvmlShutdown(cls):
            cls.initialized = False

    monkeypatch.setitem(sys.modules, "pynvml", _FakeNvml)

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="cuda",
            model_path=_create_model_file(tmp_path),
        )
    )

    assert runtime.device == "NVIDIA GeForce RTX 3060"
    assert runtime.device_full_name == "NVIDIA GeForce RTX 3060"
    assert _FakeOrtSession.last_instance.providers == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert _FakeOrtSession.last_instance.run_calls == 1


def test_cuda_runtime_rejects_missing_cuda_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CPUExecutionProvider"]

    with pytest.raises(RuntimeError, match="Requested CUDA execution provider is unavailable"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="cuda",
                model_path=_create_model_file(tmp_path),
            )
        )


def test_cuda_runtime_rejects_cpu_fallback_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CPUExecutionProvider"

    with pytest.raises(RuntimeError, match="CUDA backend initialization failed"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="cuda",
                model_path=_create_model_file(tmp_path),
            )
        )


def test_cuda_runtime_raises_when_probe_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CUDAExecutionProvider"
    _FakeOrtSession.default_fail_on_run = True

    with pytest.raises(RuntimeError, match="CUDA validation probe failed"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="cuda",
                model_path=_create_model_file(tmp_path),
            )
        )


def test_cuda_runtime_falls_back_to_cuda_label_when_nvml_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CUDAExecutionProvider"

    class _FailingNvml:
        @staticmethod
        def nvmlInit():
            raise RuntimeError("nvml unavailable")

    monkeypatch.setitem(sys.modules, "pynvml", _FailingNvml)

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="cuda",
            model_path=_create_model_file(tmp_path),
        )
    )

    assert runtime.device == "cuda"
    assert runtime.device_full_name is None
