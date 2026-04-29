import pytest

from joytag_infer.settings import JoyTagInferSettings


def test_openvino_settings_default_device_type_is_gpu(monkeypatch):
    monkeypatch.setenv("JOYTAG_INFER_BACKEND", "openvino")
    monkeypatch.delenv("JOYTAG_INFER_OPENVINO_DEVICE_TYPE", raising=False)

    settings = JoyTagInferSettings.from_env()

    assert settings.backend == "openvino"
    assert settings.openvino_device_type == "GPU"


def test_openvino_settings_accepts_cpu_and_gpu_device_types():
    cpu_settings = JoyTagInferSettings(backend="openvino", openvino_device_type="cpu")
    gpu_settings = JoyTagInferSettings(backend="openvino", openvino_device_type="gpu")

    assert cpu_settings.openvino_device_type == "CPU"
    assert gpu_settings.openvino_device_type == "GPU"


def test_openvino_settings_rejects_auto_device_type():
    with pytest.raises(ValueError, match="JOYTAG_INFER_OPENVINO_DEVICE_TYPE must be CPU or GPU"):
        JoyTagInferSettings(backend="openvino", openvino_device_type="AUTO")


def test_non_openvino_backend_keeps_device_type_without_validation():
    settings = JoyTagInferSettings(backend="cpu", openvino_device_type="AUTO")

    assert settings.backend == "cpu"
    assert settings.openvino_device_type == "AUTO"
