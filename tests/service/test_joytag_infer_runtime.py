from types import SimpleNamespace
import io
import sys

import numpy as np
import pytest

from joytag_infer import runtime as runtime_module
from joytag_infer.runtime import JoyTagOnnxRuntime
from joytag_infer.settings import JoyTagInferSettings


# ----- ORT fakes(cpu / cuda 分支用) -----


class _FakeOrtSession:
    last_instance = None
    default_execution_provider = "CPUExecutionProvider"
    default_fail_on_run = False

    def __init__(self, _model_path: str, *, providers):
        self.providers = providers
        self.disable_fallback_called = False
        self.execution_provider = self.default_execution_provider
        self.run_calls = 0
        self.run_batch_sizes: list[int] = []
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
        self.run_batch_sizes.append(batch_size)
        return [np.ones((batch_size, 768), dtype=np.float32)]


class _FakeOrtModule:
    InferenceSession = _FakeOrtSession
    available_providers = ["CPUExecutionProvider"]

    @staticmethod
    def get_available_providers():
        return list(_FakeOrtModule.available_providers)


# ----- OpenVINO fakes(openvino 原生分支用) -----


class _FakeOvPort:
    def __init__(self, name: str, shape=None):
        self._name = name
        self._shape = shape or [1, 768]

    def get_any_name(self) -> str:
        return self._name

    def get_partial_shape(self):
        return list(self._shape)


class _FakeOvModel:
    def __init__(self) -> None:
        self.inputs = [_FakeOvPort("input", [1, 3, 448, 448])]
        self.outputs = [_FakeOvPort("output", [1, 768])]
        self.reshape_calls: list[dict] = []

    def reshape(self, spec: dict) -> None:
        self.reshape_calls.append(spec)


class _FakeOvInferRequest:
    def __init__(self, compiled_model: "_FakeOvCompiledModel") -> None:
        self.compiled_model = compiled_model
        self.infer_calls = 0
        # 关键:模拟真实 OpenVINO 行为——infer 返回同一个内部 tensor 的视图,
        # 后续 infer 会覆写它。让测试有能力抓到"调用方忘了 copy"这类严重 bug。
        self._output_buffer = np.zeros((1, 768), dtype=np.float32)

    def infer(self, inputs: dict):
        self.infer_calls += 1
        if self.compiled_model.fail_on_infer:
            raise RuntimeError("probe failed")
        batch = next(iter(inputs.values()))
        batch_size = int(batch.shape[0])
        if self._output_buffer.shape[0] != batch_size:
            self._output_buffer = np.zeros((batch_size, 768), dtype=np.float32)
        # 每次 infer 写入可区分的模式:第 i 次调用把 one-hot 放在列 i,
        # 归一化后各次结果的方向也各不相同,便于验证"逐次结果被独立保留"。
        self._output_buffer.fill(0.0)
        for row in range(batch_size):
            col = (self.infer_calls * 17 + row) % 768
            self._output_buffer[row, col] = 1.0
        return {self.compiled_model.outputs[0]: self._output_buffer}


class _FakeOvCompiledModel:
    def __init__(self, output_port: _FakeOvPort) -> None:
        self.outputs = [output_port]
        self.fail_on_infer = False
        self.device: str | None = None
        self.config: dict = {}
        self.reshape_calls: list[dict] = []
        self.last_infer_request: _FakeOvInferRequest | None = None

    def create_infer_request(self) -> _FakeOvInferRequest:
        self.last_infer_request = _FakeOvInferRequest(self)
        return self.last_infer_request


class _FakeOvModule:
    save_model_calls: list[tuple] = []

    class PartialShape:
        def __init__(self, dims):
            self.dims = list(dims)

        def __repr__(self) -> str:
            return f"PartialShape({self.dims})"

    @staticmethod
    def save_model(model, path, compress_to_fp16: bool = False) -> None:
        _FakeOvModule.save_model_calls.append((model, path, compress_to_fp16))


class _FakeOpenVinoCore:
    last_instance: "_FakeOpenVinoCore | None" = None
    available_devices: list[str] = ["CPU"]
    default_fail_on_infer: bool = False

    def __init__(self) -> None:
        self.set_property_calls: list[dict] = []
        self.compile_calls: list[_FakeOvCompiledModel] = []
        self.fail_on_infer = _FakeOpenVinoCore.default_fail_on_infer
        _FakeOpenVinoCore.last_instance = self

    def get_property(self, device_name: str, property_name: str) -> str:
        assert property_name == "FULL_DEVICE_NAME"
        return f"Fake {device_name}"

    def set_property(self, prop: dict) -> None:
        self.set_property_calls.append(dict(prop))

    def read_model(self, _path: str) -> _FakeOvModel:
        return _FakeOvModel()

    def compile_model(self, model: _FakeOvModel, device: str, config: dict) -> _FakeOvCompiledModel:
        cm = _FakeOvCompiledModel(model.outputs[0])
        cm.fail_on_infer = self.fail_on_infer
        cm.device = device
        cm.config = dict(config)
        cm.reshape_calls = list(model.reshape_calls)
        self.compile_calls.append(cm)
        return cm


def _create_model_file(tmp_path) -> str:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"fake-model")
    return str(model_path)


def _make_png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color=(0, 128, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _setup_openvino_env(monkeypatch, *, devices, fail_on_infer: bool = False) -> None:
    _FakeOpenVinoCore.available_devices = list(devices)
    _FakeOpenVinoCore.default_fail_on_infer = fail_on_infer
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    monkeypatch.setattr(runtime_module, "ov", _FakeOvModule)


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    _FakeOrtSession.last_instance = None
    _FakeOrtSession.default_execution_provider = "CPUExecutionProvider"
    _FakeOrtSession.default_fail_on_run = False
    _FakeOrtModule.available_providers = ["CPUExecutionProvider"]
    _FakeOpenVinoCore.last_instance = None
    _FakeOpenVinoCore.available_devices = ["CPU"]
    _FakeOpenVinoCore.default_fail_on_infer = False
    _FakeOvModule.save_model_calls = []
    monkeypatch.delitem(sys.modules, "pynvml", raising=False)


# ----- OpenVINO 原生分支测试 -----


def test_openvino_runtime_rejects_missing_gpu_device(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU"])

    with pytest.raises(RuntimeError, match="Requested OpenVINO device GPU is unavailable"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="openvino",
                model_path=_create_model_file(tmp_path),
                openvino_device_type="GPU",
            )
        )


def test_openvino_runtime_validates_gpu_at_startup(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU", "GPU.0"])

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="GPU",
        )
    )

    assert runtime.device == "Fake GPU.0"
    assert runtime.device_full_name == "Fake GPU.0"
    core = _FakeOpenVinoCore.last_instance
    assert core is not None
    assert len(core.compile_calls) == 1
    compiled = core.compile_calls[0]
    assert compiled.device == "GPU.0"
    # LATENCY + 单流：不多分配 pinned 缓冲。
    assert compiled.config.get("PERFORMANCE_HINT") == "LATENCY"
    assert compiled.config.get("NUM_STREAMS") == "1"
    # GPU 场景必须把动态 batch 静态化到 [1,3,448,448]。
    assert compiled.reshape_calls, "GPU 分支必须静态化 batch=1"
    reshape_shape = compiled.reshape_calls[0]["input"]
    assert getattr(reshape_shape, "dims", None) == [1, 3, 448, 448]
    # 启动 probe 至少触发一次 infer。
    assert compiled.last_infer_request is not None
    assert compiled.last_infer_request.infer_calls >= 1
    # CACHE_DIR 落盘缓存已开启。
    assert any("CACHE_DIR" in call for call in core.set_property_calls)
    assert runtime.runtime_info(probe=False)["device"] == "Fake GPU.0"


def test_openvino_runtime_raises_when_gpu_probe_fails(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["GPU.0"], fail_on_infer=True)

    with pytest.raises(RuntimeError, match="OpenVINO GPU validation probe failed"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="openvino",
                model_path=_create_model_file(tmp_path),
                openvino_device_type="GPU",
            )
        )


def test_openvino_runtime_cpu_mode_uses_visible_cpu_without_gpu_probe(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU"])

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="CPU",
        )
    )

    assert runtime.device == "cpu"
    assert runtime.device_full_name == "Fake CPU"
    core = _FakeOpenVinoCore.last_instance
    assert core is not None
    compiled = core.compile_calls[0]
    # CPU 场景不做静态 reshape，保留原始动态 batch。
    assert compiled.reshape_calls == []
    # 启动流程未触发 GPU probe。
    assert compiled.last_infer_request is not None
    assert compiled.last_infer_request.infer_calls == 0


def test_openvino_runtime_persists_fp16_ir_on_first_launch(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU"])

    JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="CPU",
        )
    )

    # 首次启动应生成 FP16 IR，权重从 fp32 减半。
    assert _FakeOvModule.save_model_calls, "首次启动应尝试生成 FP16 IR"
    _, ir_path, compress_to_fp16 = _FakeOvModule.save_model_calls[0]
    assert compress_to_fp16 is True
    assert ir_path.endswith(".fp16.xml")


def test_openvino_gpu_runs_inference_one_image_at_a_time(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["GPU.0"])

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="GPU",
        )
    )

    # 核显固定逐张推理，避免动态 batch 撑大不可回收的 pinned 共享内存。
    assert runtime._infer_chunk_size == 1
    infer_request = _FakeOpenVinoCore.last_instance.compile_calls[0].last_infer_request
    assert infer_request is not None
    calls_before = infer_request.infer_calls

    vectors = runtime.embed_image_batch([_make_png_bytes() for _ in range(4)])

    assert len(vectors) == 4
    # 4 张图 -> 4 次 infer(逐张)，而非 1 次整批。
    assert infer_request.infer_calls - calls_before == 4


def test_openvino_gpu_returns_distinct_vectors_across_chunks(monkeypatch, tmp_path):
    # 回归测试:OpenVINO InferRequest 返回的输出是共享 buffer 的视图,
    # 逐张循环里若直接把视图追加进 chunks 列表,最终 concatenate 出的向量
    # 会全部退化成"最后一次 infer 的结果"。这里用 fake mock 精确复现该行为,
    # 验证 runtime._run_openvino 已经通过 .copy() 隔离每次 infer 的结果。
    _setup_openvino_env(monkeypatch, devices=["GPU.0"])
    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="GPU",
        )
    )

    vectors = runtime.embed_image_batch([_make_png_bytes() for _ in range(4)])

    assert len(vectors) == 4
    numeric = [np.asarray(vec, dtype=np.float32) for vec in vectors]
    # 4 次 infer 各自写入不同列的 one-hot,归一化后方向也不同。
    # 若 runtime 没有 copy,4 个向量会一模一样。
    for i in range(len(numeric) - 1):
        assert not np.allclose(numeric[i], numeric[i + 1]), (
            f"vectors {i} and {i + 1} are identical — output buffer was not copied per chunk"
        )


def test_openvino_reads_existing_fp16_ir_without_regenerating(monkeypatch, tmp_path):
    # 二次启动:.fp16.xml 已存在,应直接读取,不再触发 save_model。
    _setup_openvino_env(monkeypatch, devices=["CPU"])
    model_path = _create_model_file(tmp_path)
    # 预置一个"已存在"的 IR 文件,mock read_model 不校验内容。
    ir_path = tmp_path / "model.fp16.xml"
    ir_path.write_bytes(b"fake-ir")

    JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=model_path,
            openvino_device_type="CPU",
        )
    )

    assert _FakeOvModule.save_model_calls == [], (
        "第二次启动不应再重新生成 FP16 IR"
    )


def test_openvino_falls_back_to_onnx_when_ir_save_fails(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU"])

    def _raise(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_FakeOvModule, "save_model", _raise)

    # 不应抛异常:save_model 失败应软降级到用 ONNX 模型编译。
    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="CPU",
        )
    )

    assert runtime.execution_provider == "OpenVINOExecutionProvider"
    # 依然应完成 compile。
    assert _FakeOpenVinoCore.last_instance is not None
    assert len(_FakeOpenVinoCore.last_instance.compile_calls) == 1


def test_openvino_raises_clear_error_when_ov_module_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "OpenVinoCore", _FakeOpenVinoCore)
    monkeypatch.setattr(runtime_module, "ov", None)
    _FakeOpenVinoCore.available_devices = ["CPU"]

    with pytest.raises(RuntimeError, match="OpenVINO Python runtime is not installed"):
        JoyTagOnnxRuntime(
            JoyTagInferSettings(
                backend="openvino",
                model_path=_create_model_file(tmp_path),
                openvino_device_type="CPU",
            )
        )


def test_openvino_cpu_runs_inference_in_single_batch(monkeypatch, tmp_path):
    _setup_openvino_env(monkeypatch, devices=["CPU"])

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="openvino",
            model_path=_create_model_file(tmp_path),
            openvino_device_type="CPU",
        )
    )

    # CPU 路径不拆分，保持整批一次推理。
    assert runtime._infer_chunk_size is None
    infer_request = _FakeOpenVinoCore.last_instance.compile_calls[0].last_infer_request
    assert infer_request is not None
    calls_before = infer_request.infer_calls

    vectors = runtime.embed_image_batch([_make_png_bytes() for _ in range(4)])

    assert len(vectors) == 4
    assert infer_request.infer_calls - calls_before == 1


# ----- ORT CPU 分支测试 -----


def test_cpu_runs_inference_one_image_at_a_time(monkeypatch, tmp_path):
    # 回归测试:上游 inference_batch_size=16 会把整批打到服务端,而 ViT-448 的 attention
    # 中间张量按 batch×seq² 增长,整批推理的峰值内存足以打爆容器。CPU 后端必须逐张跑。
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CPUExecutionProvider"

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="cpu",
            model_path=_create_model_file(tmp_path),
        )
    )

    assert runtime._infer_chunk_size == 1
    session = _FakeOrtSession.last_instance
    run_calls_before = session.run_calls
    session.run_batch_sizes.clear()

    vectors = runtime.embed_image_batch([_make_png_bytes() for _ in range(16)])

    assert len(vectors) == 16
    # 16 张图 -> 16 次 infer，每次只喂 1 张，而非 1 次整批 16 张。
    assert session.run_calls - run_calls_before == 16
    assert session.run_batch_sizes == [1] * 16


# ----- CUDA 分支测试(仍走 ORT,不改) -----


def test_cuda_runs_inference_in_single_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "ort", _FakeOrtModule)
    _FakeOrtModule.available_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    _FakeOrtSession.default_execution_provider = "CUDAExecutionProvider"

    runtime = JoyTagOnnxRuntime(
        JoyTagInferSettings(
            backend="cuda",
            model_path=_create_model_file(tmp_path),
        )
    )

    # 独显整批推理有吞吐收益，保持原行为不拆分。
    assert runtime._infer_chunk_size is None
    session = _FakeOrtSession.last_instance
    run_calls_before = session.run_calls
    vectors = runtime.embed_image_batch([_make_png_bytes() for _ in range(4)])

    assert len(vectors) == 4
    assert session.run_calls - run_calls_before == 1


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
