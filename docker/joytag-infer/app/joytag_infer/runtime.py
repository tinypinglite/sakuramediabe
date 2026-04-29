import io
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
    from openvino import Core as OpenVinoCore
except ImportError:
    try:
        from openvino.runtime import Core as OpenVinoCore
    except ImportError:
        OpenVinoCore = None


def _bicubic_resample():
    return getattr(Image, "Resampling", Image).BICUBIC


def _l2_normalize_vector(vector: np.ndarray) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        raise ValueError("vector is empty")
    norm = float(np.linalg.norm(arr))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("vector norm must be a positive finite number")
    return arr / norm


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

        if ort is None:
            raise RuntimeError("onnxruntime is not installed")
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Missing required file: {self.model_path}")
        if self.backend == "openvino":
            self._inspect_openvino_device()

        self.available_providers = [str(item) for item in list(ort.get_available_providers() or [])]
        providers = self._build_providers()
        self.session = ort.InferenceSession(str(self.model_path), providers=providers)
        if (
            self.backend == "openvino"
            and self.settings.openvino_device_type == "GPU"
            and hasattr(self.session, "disable_fallback")
        ):
            # GPU 模式必须是硬约束，不允许 ORT 静默回退到 CPU。
            self.session.disable_fallback()
        self.input_name = str(self.session.get_inputs()[0].name)
        self.output_name = str(self.session.get_outputs()[0].name)
        self.execution_provider = str(self.session.get_providers()[0])
        if self.backend == "openvino" and self.execution_provider != "OpenVINOExecutionProvider":
            raise RuntimeError(
                "OpenVINO backend initialization failed: "
                f"unexpected execution provider {self.execution_provider}"
            )
        self.device = self._resolve_device()
        self.vector_size = self._resolve_vector_size()
        if self.backend == "openvino" and self.settings.openvino_device_type == "GPU":
            self._validate_openvino_gpu_probe()

    def _build_providers(self) -> list[str | tuple[str, dict[str, Any]]]:
        if self.backend == "cpu":
            return ["CPUExecutionProvider"]
        if self.backend == "openvino":
            return [
                (
                    "OpenVINOExecutionProvider",
                    {"device_type": self.settings.openvino_device_type},
                )
            ]
        if self.backend == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        raise RuntimeError(f"Unsupported JOYTAG_INFER_BACKEND: {self.backend}")

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

    def _resolve_device(self) -> str:
        if self.execution_provider == "CUDAExecutionProvider":
            return "CUDA"
        if self.execution_provider == "OpenVINOExecutionProvider":
            if self._openvino_selected_device_name and self._match_openvino_device_family(
                self._openvino_selected_device_name,
                "GPU",
            ):
                return "GPU"
            if self._openvino_selected_device_name and self._match_openvino_device_family(
                self._openvino_selected_device_name,
                "CPU",
            ):
                return "CPU"
            return self.settings.openvino_device_type
        return "CPU"

    def _resolve_vector_size(self) -> int:
        output = self.session.get_outputs()[0]
        shape = list(output.shape or [])
        if len(shape) < 2:
            raise RuntimeError(f"Unexpected JoyTag output shape: {shape}")
        feature_dim = shape[-1]
        if feature_dim in (None, "None"):
            probe = self._probe_vector()
            return int(probe.shape[0])
        vector_size = int(feature_dim)
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
            raise ValueError(f"Invalid image bytes: {exc}") from exc
        image = image.resize((image_size, image_size), _bicubic_resample())
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))
        return np.expand_dims(arr, axis=0)

    def embed_image_bytes(self, image_bytes: bytes) -> list[float]:
        results = self.embed_image_batch([image_bytes])
        return results[0]

    def embed_image_batch(self, image_bytes_list: list[bytes]) -> list[list[float]]:
        if not image_bytes_list:
            return []
        inputs = [
            self._preprocess_image_bytes(image_bytes, image_size=self.image_size)
            for image_bytes in image_bytes_list
        ]
        batch = np.concatenate(inputs, axis=0).astype(np.float32, copy=False)
        with self._infer_lock:
            outputs = self.session.run([self.output_name], {self.input_name: batch})
        vector_array = np.asarray(outputs[0], dtype=np.float32)
        if vector_array.ndim != 2:
            raise RuntimeError(f"Unexpected JoyTag output rank: {vector_array.shape}")
        if vector_array.shape[1] != self.vector_size:
            raise RuntimeError(
                f"JoyTag vector size mismatch: expected={self.vector_size}, actual={vector_array.shape[1]}"
            )
        # 服务端统一做 L2 归一化，确保主服务与向量库口径一致。
        return [
            _l2_normalize_vector(vector).astype(float).tolist()
            for vector in vector_array
        ]

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
