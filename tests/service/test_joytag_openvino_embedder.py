import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from src.service.discovery.joytag_openvino_embedder import JoyTagOpenVinoEmbedder


class _FakeFeatureDim:
    def __init__(self, length: int):
        self.length = length

    def is_dynamic(self):
        return False

    def get_length(self):
        return self.length


class _FakeOutputPort:
    def __init__(self, vector_size: int):
        self.partial_shape = [1, _FakeFeatureDim(vector_size)]


class _FakeInferRequest:
    def __init__(self, output_port, vector):
        self.output_port = output_port
        self.vector = vector

    def infer(self, _inputs):
        return {self.output_port: np.asarray([self.vector], dtype=np.float32)}


class _FakeCompiledModel:
    def __init__(self, output_port, vector):
        self._output_port = output_port
        self._vector = vector

    def create_infer_request(self):
        return _FakeInferRequest(self._output_port, self._vector)

    def input(self, _index):
        return "input"

    def output(self, _index):
        return self._output_port


class _FakeCore:
    def __init__(self, *, available_devices, gpu_error=None, cpu_error=None, vector_size=3):
        self.available_devices = available_devices
        self.gpu_error = gpu_error
        self.cpu_error = cpu_error
        self.vector_size = vector_size

    def read_model(self, _model_path):
        return "model"

    def compile_model(self, _model, device, _config=None):
        if device == "GPU" and self.gpu_error is not None:
            raise self.gpu_error
        if device == "CPU" and self.cpu_error is not None:
            raise self.cpu_error
        return _FakeCompiledModel(_FakeOutputPort(self.vector_size), [1.0, 2.0, 2.0])


def _build_image_bytes() -> bytes:
    image = Image.new("RGB", (4, 4), color=(255, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _create_model_dir(tmp_path):
    model_dir = tmp_path / "joytag"
    model_dir.mkdir()
    (model_dir / "model_vit_768.onnx").write_bytes(b"fake-model")
    return model_dir


def test_embedder_requires_model_files(tmp_path):
    model_dir = tmp_path / "joytag"
    model_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        JoyTagOpenVinoEmbedder(model_dir=model_dir)


def test_embedder_prefers_gpu_when_available(monkeypatch, tmp_path):
    model_dir = _create_model_dir(tmp_path)
    fake_core = _FakeCore(available_devices=["GPU", "CPU"])
    monkeypatch.setattr(
        "src.service.discovery.joytag_openvino_embedder.ov",
        SimpleNamespace(Core=lambda: fake_core),
    )

    embedder = JoyTagOpenVinoEmbedder(model_dir=model_dir, prefer_gpu=True)
    result = embedder.infer_image_bytes(_build_image_bytes())

    assert embedder.used_device == "GPU"
    assert embedder.vector_size == 3
    assert pytest.approx(sum(value * value for value in result.vector), rel=1e-6) == 1.0


def test_embedder_falls_back_to_cpu_when_gpu_compile_fails(monkeypatch, tmp_path):
    model_dir = _create_model_dir(tmp_path)
    fake_core = _FakeCore(
        available_devices=["GPU", "CPU"],
        gpu_error=RuntimeError("gpu unavailable"),
    )
    monkeypatch.setattr(
        "src.service.discovery.joytag_openvino_embedder.ov",
        SimpleNamespace(Core=lambda: fake_core),
    )

    embedder = JoyTagOpenVinoEmbedder(model_dir=model_dir, prefer_gpu=True)

    assert embedder.used_device == "CPU"
