import io
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from src.config.config import settings

try:
    import openvino as ov
except ImportError:
    ov = None


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


class JoyTagEmbeddingResult(BaseModel):
    vector: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class JoyTagOpenVinoEmbedder:
    model_name = "joytag-openvino"
    default_image_size = 448

    def __init__(
        self,
        model_dir: Path | str | None = None,
        cpu_threads: int | None = None,
        prefer_gpu: bool | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        configured_model_dir = model_dir or settings.image_search.joytag_model_dir
        resolved_model_dir = Path(configured_model_dir)
        if not resolved_model_dir.is_absolute():
            resolved_model_dir = repo_root / resolved_model_dir
        self.model_dir = resolved_model_dir
        self.prefer_gpu = (
            settings.image_search.prefer_gpu if prefer_gpu is None else bool(prefer_gpu)
        )
        self.cpu_threads = self._resolve_cpu_threads(
            settings.image_search.cpu_threads if cpu_threads is None else cpu_threads
        )
        self._infer_lock = threading.Lock()

        model_path = self.model_dir / "model_vit_768.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"Missing required file: {model_path}")
        self.image_size = self.default_image_size

        if ov is None:
            raise RuntimeError("openvino is not installed")

        self._core = ov.Core()
        self.available_devices = self._safe_available_devices()
        self._model = self._core.read_model(str(model_path))
        self._compiled_model, self.used_device = self._compile_model_with_fallback()
        self._infer_request = self._compiled_model.create_infer_request()
        self._input_port = self._compiled_model.input(0)
        self._output_port = self._compiled_model.output(0)
        self.vector_size = self._resolve_vector_size(self._output_port)
        logger.info(
            "JoyTagOpenVinoEmbedder initialized with device={} vector_size={} image_size={} available_devices={}",
            self.used_device,
            self.vector_size,
            self.image_size,
            self.available_devices,
        )

    @staticmethod
    def _resolve_cpu_threads(cpu_threads: int | None) -> int:
        if cpu_threads is not None and cpu_threads > 0:
            return cpu_threads
        detected = os.cpu_count() or 1
        return max(1, detected // 4)

    def _safe_available_devices(self) -> list[str]:
        try:
            return list(self._core.available_devices)
        except Exception:
            return []

    def _compile_model_with_fallback(self):
        errors: list[str] = []
        devices = ["GPU", "CPU"] if self.prefer_gpu else ["CPU"]
        for device in devices:
            if device == "GPU":
                if self.available_devices and "GPU" not in self.available_devices:
                    continue
                try:
                    return self._core.compile_model(self._model, "GPU"), "GPU"
                except Exception as exc:
                    errors.append(f"GPU: {exc}")
                    continue

            try:
                return self._compile_cpu(), "CPU"
            except Exception as exc:
                errors.append(f"CPU: {exc}")
        raise RuntimeError("Failed to compile JoyTag model. " + " | ".join(errors))

    def _compile_cpu(self):
        config = {"INFERENCE_NUM_THREADS": self.cpu_threads}
        try:
            return self._core.compile_model(self._model, "CPU", config)
        except Exception:
            return self._core.compile_model(self._model, "CPU")

    @staticmethod
    def _resolve_vector_size(output_port) -> int:
        partial_shape = getattr(output_port, "partial_shape", None)
        if partial_shape is None:
            raise RuntimeError("Unable to resolve JoyTag output shape")
        try:
            dimensions = list(partial_shape)
        except Exception as exc:
            raise RuntimeError(f"Unable to parse JoyTag output shape: {partial_shape}") from exc
        if len(dimensions) < 2:
            raise RuntimeError(f"Unexpected JoyTag output rank: {partial_shape}")

        feature_dim = dimensions[-1]
        is_dynamic = getattr(feature_dim, "is_dynamic", None)
        if callable(is_dynamic):
            is_dynamic = bool(is_dynamic())
        elif is_dynamic is None:
            is_dynamic = False
        else:
            is_dynamic = bool(is_dynamic)
        if is_dynamic:
            raise RuntimeError(f"JoyTag output feature dimension is dynamic: {partial_shape}")

        get_length = getattr(feature_dim, "get_length", None)
        vector_size = int(get_length()) if callable(get_length) else int(feature_dim)
        if vector_size <= 0:
            raise RuntimeError(f"Invalid JoyTag vector_size from output shape: {partial_shape}")
        return vector_size

    @staticmethod
    def _preprocess_image_bytes(image_bytes: bytes, size: int) -> np.ndarray:
        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError(f"Invalid image bytes: {exc}") from exc
        image = image.resize((size, size), _bicubic_resample())
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))
        return np.expand_dims(arr, axis=0)

    def infer_image_bytes(self, image_bytes: bytes) -> JoyTagEmbeddingResult:
        inp = self._preprocess_image_bytes(image_bytes, size=self.image_size).astype(
            np.float32,
            copy=False,
        )
        with self._infer_lock:
            try:
                result = self._infer_request.infer({self._input_port: inp})
            except Exception as exc:
                raise RuntimeError(f"JoyTag inference failed: {exc}") from exc
        try:
            vector = np.asarray(result[self._output_port], dtype=np.float32).reshape(-1)
        except Exception as exc:
            raise RuntimeError(f"Unexpected JoyTag output: {exc}") from exc
        if int(vector.shape[0]) != int(self.vector_size):
            raise RuntimeError(
                f"JoyTag vector size mismatch: expected={self.vector_size}, actual={vector.shape[0]}"
            )
        normalized = _l2_normalize_vector(vector).astype(np.float32, copy=False)
        return JoyTagEmbeddingResult(
            vector=normalized.astype(float).tolist(),
            metadata={
                "provider": "joytag",
                "device": self.used_device,
                "image_size": int(self.image_size),
                "vector_size": int(self.vector_size),
            },
        )


@lru_cache(maxsize=1)
def get_joytag_openvino_embedder() -> JoyTagOpenVinoEmbedder:
    return JoyTagOpenVinoEmbedder()
