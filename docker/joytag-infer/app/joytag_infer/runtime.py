import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from joytag_infer.settings import JoyTagInferSettings

try:
    import onnxruntime as ort
except ImportError:
    ort = None

try:
    import openvino as ov
    from openvino import Core as OpenVinoCore
except ImportError:
    try:
        import openvino.runtime as ov  # type: ignore[no-redef]
        from openvino.runtime import Core as OpenVinoCore
    except ImportError:
        ov = None  # type: ignore[assignment]
        OpenVinoCore = None


_logger = logging.getLogger(__name__)


def _bicubic_resample():
    return getattr(Image, "Resampling", Image).BICUBIC


class InvalidImageError(ValueError):
    """输入图片无法解码，属于调用方送进来的数据问题。"""


class DegenerateVectorError(ValueError):
    """模型输出无法归一化（含 NaN/inf 或范数为零），属于服务端推理故障。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _l2_normalize_vector(vector: np.ndarray) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        raise DegenerateVectorError("empty_vector", "degenerate embedding: reason=empty_vector dim=0")
    nan_count = int(np.count_nonzero(np.isnan(arr)))
    inf_count = int(np.count_nonzero(np.isinf(arr)))
    # 统计量只在有限元素上算：只要有一个 NaN，min/max/mean 会全部变成 NaN，日志就失去诊断价值。
    abs_finite = np.abs(arr[np.isfinite(arr)])
    abs_max = float(abs_finite.max()) if abs_finite.size else 0.0
    # 均值走 float64 累加：元素量级本就逼近 float32 上限时，float32 求和会把统计量自己烧成 inf。
    abs_mean = float(abs_finite.mean(dtype=np.float64)) if abs_finite.size else 0.0
    norm = float(np.linalg.norm(arr))
    # 四种退化的排查方向完全不同，必须分开报：
    #   nan/inf        -> 模型内部数值故障（kernel、精度、输入分布）
    #   norm_overflow  -> 元素本身有限，但平方和在 float32 下溢出，说明激活量级已经失控
    #   zero_vector    -> 模型确实输出了全零，跟数值溢出无关
    if nan_count:
        reason = "nan"
    elif inf_count:
        reason = "inf"
    elif not np.isfinite(norm):
        reason = "norm_overflow"
    elif norm <= 0.0:
        reason = "zero_vector"
    else:
        return arr / norm
    raise DegenerateVectorError(
        reason,
        f"degenerate embedding: reason={reason} dim={arr.size} "
        f"nan_count={nan_count} inf_count={inf_count} "
        f"finite_abs_max={abs_max:.6g} finite_abs_mean={abs_mean:.6g} norm={norm:.6g}",
    )


class JoyTagOnnxRuntime:
    model_name = "joytag-onnxruntime"

    def __init__(self, settings: JoyTagInferSettings | None = None) -> None:
        self.settings = settings or JoyTagInferSettings.from_env()
        self.image_size = int(self.settings.image_size)
        self.model_path = Path(self.settings.model_path)
        self.backend = str(self.settings.backend)
        self._infer_lock = threading.Lock()
        self.device_full_name: str | None = None
        self._openvino_visible_devices: list[str] = []
        self._openvino_selected_device_name: str | None = None
        self._openvino_core: Any = None

        # 二选一的推理引擎持有物：openvino 走原生 OV，cpu/cuda 走 ORT。
        self.session: Any = None
        self._ov_compiled_model: Any = None
        self._ov_infer_request: Any = None
        self._ov_output_port: Any = None
        self._output_shape: list[Any] = []

        if not self.model_path.is_file():
            raise FileNotFoundError(f"Missing required file: {self.model_path}")

        if self.backend == "openvino":
            self._inspect_openvino_device()
            self._init_openvino_native()
        elif self.backend in ("cpu", "cuda"):
            self._init_ort_backend()
        else:
            raise RuntimeError(f"Unsupported JOYTAG_INFER_BACKEND: {self.backend}")

        self._infer_chunk_size = self._resolve_infer_chunk_size()
        self.device = self._resolve_device()
        self.vector_size = self._resolve_vector_size()
        if self.backend == "openvino" and self.settings.openvino_device_type == "GPU":
            self._validate_openvino_gpu_probe()
        if self.backend == "cuda":
            self._validate_cuda_probe()

    # ------------------------------------------------------------------
    # ORT 分支：cpu / cuda
    # ------------------------------------------------------------------

    def _init_ort_backend(self) -> None:
        if ort is None:
            raise RuntimeError("onnxruntime is not installed")
        self.available_providers = [str(item) for item in list(ort.get_available_providers() or [])]
        if self.backend == "cuda":
            self._validate_cuda_provider_available()
        providers = self._build_ort_providers()
        self.session = ort.InferenceSession(str(self.model_path), providers=providers)
        self.input_name = str(self.session.get_inputs()[0].name)
        self.output_name = str(self.session.get_outputs()[0].name)
        self.execution_provider = str(self.session.get_providers()[0])
        if self.backend == "cuda" and self.execution_provider != "CUDAExecutionProvider":
            raise RuntimeError(
                "CUDA backend initialization failed: "
                f"unexpected execution provider {self.execution_provider}"
            )
        self._output_shape = list(self.session.get_outputs()[0].shape or [])

    def _build_ort_providers(self) -> list[str | tuple[str, dict[str, Any]]]:
        if self.backend == "cpu":
            return ["CPUExecutionProvider"]
        if self.backend == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        raise RuntimeError(f"ORT backend does not support: {self.backend}")

    def _validate_cuda_provider_available(self) -> None:
        if "CUDAExecutionProvider" in self.available_providers:
            return
        visible_providers = ", ".join(self.available_providers) or "<none>"
        raise RuntimeError(
            "Requested CUDA execution provider is unavailable. "
            f"Visible providers: {visible_providers}"
        )

    def _validate_cuda_probe(self) -> None:
        try:
            self._probe_vector()
        except Exception as exc:
            raise RuntimeError(f"CUDA validation probe failed: {exc}") from exc

    # ------------------------------------------------------------------
    # OpenVINO 原生分支：openvino
    # ------------------------------------------------------------------

    def _init_openvino_native(self) -> None:
        if ov is None or OpenVinoCore is None:
            raise RuntimeError("OpenVINO Python runtime is not installed")
        # 复用 _inspect_openvino_device 里已经构造好的 Core,避免二次初始化。
        core = self._openvino_core
        if core is None:
            try:
                core = OpenVinoCore()
            except Exception as exc:
                raise RuntimeError(f"Failed to initialize OpenVINO runtime: {exc}") from exc

        selected_device = self._openvino_selected_device_name or self.settings.openvino_device_type

        # 编译产物落盘缓存，避免每次冷启动重跑 4~5s 的核显编译。
        cache_dir = str(self.model_path.parent / "ov_cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            core.set_property({"CACHE_DIR": cache_dir})
        except Exception as exc:
            _logger.warning("OpenVINO CACHE_DIR unavailable dir=%s error=%s", cache_dir, exc)

        model = self._read_openvino_model(core)

        # GPU 静态化 batch=1：彻底消除动态 shape 导致的 pinned 共享内存膨胀。
        if self.settings.openvino_device_type == "GPU":
            try:
                input_port = model.inputs[0]
                model.reshape(
                    {
                        input_port.get_any_name(): ov.PartialShape(
                            [1, 3, self.image_size, self.image_size]
                        )
                    }
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to reshape model to static batch=1: {exc}") from exc

        compile_config = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1"}
        try:
            self._ov_compiled_model = core.compile_model(model, selected_device, compile_config)
        except Exception as exc:
            raise RuntimeError(f"OpenVINO compile_model failed: {exc}") from exc
        self._ov_infer_request = self._ov_compiled_model.create_infer_request()

        self.input_name = str(model.inputs[0].get_any_name())
        self.output_name = str(model.outputs[0].get_any_name())
        self._ov_output_port = self._ov_compiled_model.outputs[0]
        self._output_shape = list(model.outputs[0].get_partial_shape())

        # 原生 OV 不经过 ORT，用虚构值兼容对外 API 消费者。
        self.available_providers = ["OpenVINOExecutionProvider"]
        self.execution_provider = "OpenVINOExecutionProvider"

    def _read_openvino_model(self, core: Any) -> Any:
        if self.model_path.suffix.lower() == ".xml":
            return core.read_model(str(self.model_path))
        # 复用/生成同目录 FP16 IR，权重从 fp32 减半。
        ir_xml = self.model_path.with_suffix(".fp16.xml")
        if ir_xml.is_file():
            try:
                return core.read_model(str(ir_xml))
            except Exception as exc:
                _logger.warning("Failed to read FP16 IR path=%s error=%s", ir_xml, exc)
        model = core.read_model(str(self.model_path))
        # 覆盖前先清理可能残留的部分文件,避免 write-then-crash 留下的损坏 IR 反复挡道。
        for stale in (ir_xml, ir_xml.with_suffix(".bin")):
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                _logger.warning("Failed to unlink stale IR path=%s error=%s", stale, exc)
        try:
            ov.save_model(model, str(ir_xml), compress_to_fp16=True)
            _logger.info("Saved FP16 IR path=%s", ir_xml)
            return core.read_model(str(ir_xml))
        except Exception as exc:
            _logger.warning(
                "Failed to save FP16 IR path=%s error=%s (falling back to ONNX)", ir_xml, exc
            )
            return model

    def _validate_openvino_gpu_probe(self) -> None:
        if not self._openvino_selected_device_name or not self._match_openvino_device_family(
            self._openvino_selected_device_name,
            "GPU",
        ):
            raise RuntimeError("OpenVINO GPU validation failed: GPU device is not available")
        try:
            self._probe_vector()
        except Exception as exc:
            raise RuntimeError(f"OpenVINO GPU validation probe failed: {exc}") from exc

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------

    @staticmethod
    def _match_openvino_device_family(device_name: str, family: str) -> bool:
        normalized_name = str(device_name).strip().upper()
        normalized_family = str(family).strip().upper()
        return normalized_name == normalized_family or normalized_name.startswith(f"{normalized_family}.")

    def _inspect_openvino_device(self) -> None:
        if OpenVinoCore is None:
            raise RuntimeError("OpenVINO Python runtime is not installed")
        try:
            core = OpenVinoCore()
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize OpenVINO runtime: {exc}") from exc
        self._openvino_core = core

        raw_devices = [str(item).strip() for item in list(getattr(core, "available_devices", []) or [])]
        self._openvino_visible_devices = [device for device in raw_devices if device]
        requested_device = self.settings.openvino_device_type
        selected_device_name = next(
            (
                device_name
                for device_name in self._openvino_visible_devices
                if self._match_openvino_device_family(device_name, requested_device)
            ),
            None,
        )
        if selected_device_name is None:
            visible_devices = ", ".join(self._openvino_visible_devices) or "<none>"
            raise RuntimeError(
                f"Requested OpenVINO device {requested_device} is unavailable. "
                f"Visible devices: {visible_devices}"
            )
        self._openvino_selected_device_name = selected_device_name
        try:
            full_name = core.get_property(selected_device_name, "FULL_DEVICE_NAME")
        except Exception:
            full_name = None
        self.device_full_name = str(full_name) if full_name else None

    @staticmethod
    def _resolve_cuda_device_name() -> str | None:
        try:
            import pynvml
        except ImportError:
            return None

        initialized = False
        try:
            pynvml.nvmlInit()
            initialized = True
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="ignore")
            normalized_name = str(name).strip()
            return normalized_name or None
        except Exception:
            return None
        finally:
            if initialized:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass

    def _resolve_device(self) -> str:
        if self.execution_provider == "CUDAExecutionProvider":
            cuda_device_name = self._resolve_cuda_device_name()
            if cuda_device_name:
                self.device_full_name = cuda_device_name
                return cuda_device_name
            return "cuda"
        if self.execution_provider == "OpenVINOExecutionProvider":
            if self._openvino_selected_device_name and self._match_openvino_device_family(
                self._openvino_selected_device_name,
                "GPU",
            ):
                return self.device_full_name or "gpu"
            if self._openvino_selected_device_name and self._match_openvino_device_family(
                self._openvino_selected_device_name,
                "CPU",
            ):
                return "cpu"
            return str(self.settings.openvino_device_type).lower()
        return "cpu"

    def _resolve_infer_chunk_size(self) -> int | None:
        # OpenVINO(核显与 CPU)、ORT CPU 一律逐张：ViT-448 的 attention 中间张量按
        # batch×seq² 增长，上游 inference_batch_size=16 整批喂入会把峰值内存推到 GB
        # 量级并触发 OOM，而这两类设备本就跑不满并行，整批也换不到吞吐。
        # 核显额外静态化了 batch=1，推理端更是只能逐张。
        # 独显(CUDA)显存充裕且整批有吞吐收益，保持不拆。
        if self.backend in ("openvino", "cpu"):
            return 1
        return None

    def _resolve_vector_size(self) -> int:
        shape = list(self._output_shape or [])
        if len(shape) < 2:
            raise RuntimeError(f"Unexpected JoyTag output shape: {shape}")
        feature_dim = shape[-1]
        # openvino Dimension 对象：静态直接取值，动态回退到 probe。
        if hasattr(feature_dim, "is_static"):
            try:
                feature_dim = int(feature_dim.get_length()) if feature_dim.is_static else None
            except Exception:
                feature_dim = None
        if feature_dim in (None, "None"):
            probe = self._probe_vector()
            return int(probe.shape[0])
        try:
            vector_size = int(feature_dim)
        except (TypeError, ValueError):
            probe = self._probe_vector()
            return int(probe.shape[0])
        if vector_size <= 0:
            raise RuntimeError(f"Invalid JoyTag vector size: {shape}")
        return vector_size

    def _probe_vector(self) -> np.ndarray:
        image = Image.new("RGB", (8, 8), color=(255, 0, 0))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return np.asarray(self.embed_image_bytes(output.getvalue()), dtype=np.float32)

    @staticmethod
    def _preprocess_image_bytes(image_bytes: bytes, *, image_size: int) -> np.ndarray:
        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImageError(f"Invalid image bytes: {exc}") from exc
        image = image.resize((image_size, image_size), _bicubic_resample())
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))
        return np.expand_dims(arr, axis=0)

    def embed_image_bytes(self, image_bytes: bytes) -> list[float]:
        outcome = self.embed_image_batch([image_bytes])[0]
        # 单图入口（含启动探针）保持严格失败语义，不把异常当结果往上传。
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def embed_image_batch(self, image_bytes_list: list[bytes]) -> list[list[float] | ValueError]:
        """逐项返回结果：成功是向量，失败是对应的异常对象。

        单张图的解码失败或输出退化只影响它自己，不再连坐同批其他图片——
        上游索引任务据此把坏图标记 FAILED 并继续，而不是整个任务中止。
        """
        if not image_bytes_list:
            return []
        batch_size = len(image_bytes_list)
        outcomes: list[Any] = [None] * batch_size
        inputs: list[np.ndarray] = []
        valid_positions: list[int] = []
        for position, image_bytes in enumerate(image_bytes_list):
            try:
                inputs.append(self._preprocess_image_bytes(image_bytes, image_size=self.image_size))
            except InvalidImageError as exc:
                _logger.warning(
                    "JoyTag image decode failed batch_index=%d batch_size=%d detail=%s",
                    position,
                    batch_size,
                    exc,
                )
                outcomes[position] = exc
                continue
            valid_positions.append(position)
        if not valid_positions:
            return outcomes

        batch = np.concatenate(inputs, axis=0).astype(np.float32, copy=False)
        started_at = time.perf_counter()
        with self._infer_lock:
            vector_array = self._run_inference(batch)
        infer_ms = int((time.perf_counter() - started_at) * 1000)
        # 输出形状不符属于模型/配置级故障，不是单张图的问题，整批失败才是正确语义。
        if vector_array.ndim != 2:
            raise RuntimeError(f"Unexpected JoyTag output rank: {vector_array.shape}")
        if vector_array.shape[0] != len(valid_positions):
            raise RuntimeError(
                f"JoyTag output batch mismatch: expected={len(valid_positions)}, actual={vector_array.shape[0]}"
            )
        if vector_array.shape[1] != self.vector_size:
            raise RuntimeError(
                f"JoyTag vector size mismatch: expected={self.vector_size}, actual={vector_array.shape[1]}"
            )
        _logger.info(
            "JoyTag inference done batch_size=%d infer_ms=%d device=%s",
            len(valid_positions),
            infer_ms,
            self.device,
        )
        # 服务端统一 L2 归一化，与向量库口径保持一致。
        for offset, position in enumerate(valid_positions):
            try:
                outcomes[position] = _l2_normalize_vector(vector_array[offset]).astype(float).tolist()
            except DegenerateVectorError as exc:
                # 连输入侧统计一起打：input_std 接近 0 说明这是张纯色/近纯色图，
                # 退化源在输入而非 GPU 数值故障，两种结论的后续动作完全不同。
                sample = batch[offset]
                _logger.warning(
                    "JoyTag embedding degenerate batch_index=%d batch_size=%d device=%s %s "
                    "input_min=%.4f input_max=%.4f input_mean=%.4f input_std=%.4f",
                    position,
                    batch_size,
                    self.device,
                    exc,
                    float(sample.min()),
                    float(sample.max()),
                    float(sample.mean()),
                    float(sample.std()),
                )
                outcomes[position] = exc
        return outcomes

    def _run_inference(self, batch: np.ndarray) -> np.ndarray:
        if self._ov_infer_request is not None:
            return self._run_openvino(batch)
        return self._run_ort(batch)

    def _run_ort(self, batch: np.ndarray) -> np.ndarray:
        chunk_size = self._infer_chunk_size
        if not chunk_size or chunk_size >= batch.shape[0]:
            outputs = self.session.run([self.output_name], {self.input_name: batch})
            return np.asarray(outputs[0], dtype=np.float32)
        chunks: list[np.ndarray] = []
        for start in range(0, batch.shape[0], chunk_size):
            sub_batch = batch[start : start + chunk_size]
            outputs = self.session.run([self.output_name], {self.input_name: sub_batch})
            chunks.append(np.asarray(outputs[0], dtype=np.float32))
        return np.concatenate(chunks, axis=0)

    def _run_openvino(self, batch: np.ndarray) -> np.ndarray:
        # OpenVINO InferRequest 的输出 tensor 是同一块内部 buffer 的视图，下一次 infer 会覆写它。
        # 必须立即 .copy() 到独立内存，否则:
        #   - 逐块循环里 chunks[] 全部指向最后一次 infer 的 buffer,拼出来是 N 份重复向量;
        #   - 即便单次 infer,并发调用者拿到 view 后,后续请求也会踩坏它。
        chunk_size = self._infer_chunk_size
        if not chunk_size or chunk_size >= batch.shape[0]:
            result = self._ov_infer_request.infer({self.input_name: batch})
            return np.array(result[self._ov_output_port], dtype=np.float32, copy=True)
        chunks: list[np.ndarray] = []
        for start in range(0, batch.shape[0], chunk_size):
            sub_batch = batch[start : start + chunk_size]
            result = self._ov_infer_request.infer({self.input_name: sub_batch})
            chunks.append(np.array(result[self._ov_output_port], dtype=np.float32, copy=True))
        return np.concatenate(chunks, axis=0)

    def runtime_info(self, *, probe: bool = True) -> dict[str, Any]:
        probe_latency_ms: int | None = None
        if probe:
            started_at = time.perf_counter()
            self._probe_vector()
            probe_latency_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "backend": self.backend,
            "execution_provider": self.execution_provider,
            "device": self.device,
            "device_full_name": self.device_full_name,
            "vector_size": int(self.vector_size),
            "image_size": int(self.image_size),
            "model_name": self.model_name,
            "model_path": str(self.model_path),
            "available_providers": list(self.available_providers),
            "probe_latency_ms": probe_latency_ms,
        }
