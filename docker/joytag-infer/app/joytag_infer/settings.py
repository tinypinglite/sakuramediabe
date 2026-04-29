import os
from dataclasses import dataclass


@dataclass(slots=True)
class JoyTagInferSettings:
    backend: str = "cpu"
    model_path: str = "/data/lib/joytag/model_vit_768.onnx"
    api_key: str | None = None
    openvino_device_type: str = "GPU"
    image_size: int = 448
    host: str = "0.0.0.0"
    port: int = 8001

    def __post_init__(self) -> None:
        self.backend = str(self.backend).strip().lower()
        self.model_path = str(self.model_path).strip()
        self.host = str(self.host).strip()
        self.port = int(self.port)
        self.image_size = int(self.image_size)
        self.api_key = (str(self.api_key).strip() if self.api_key is not None else "") or None

        raw_device_type = str(self.openvino_device_type or "GPU").strip().upper()
        if self.backend == "openvino":
            if raw_device_type not in {"CPU", "GPU"}:
                raise ValueError(
                    "JOYTAG_INFER_OPENVINO_DEVICE_TYPE must be CPU or GPU when "
                    "JOYTAG_INFER_BACKEND=openvino"
                )
        self.openvino_device_type = raw_device_type or "GPU"

    @classmethod
    def from_env(cls) -> "JoyTagInferSettings":
        return cls(
            backend=(os.getenv("JOYTAG_INFER_BACKEND") or "cpu").strip().lower(),
            model_path=(os.getenv("JOYTAG_INFER_MODEL_PATH") or "/data/lib/joytag/model_vit_768.onnx").strip(),
            api_key=(os.getenv("JOYTAG_INFER_API_KEY") or "").strip() or None,
            openvino_device_type=(os.getenv("JOYTAG_INFER_OPENVINO_DEVICE_TYPE") or "GPU").strip().upper(),
            image_size=int(os.getenv("JOYTAG_INFER_IMAGE_SIZE") or "448"),
            host=(os.getenv("JOYTAG_INFER_HOST") or "0.0.0.0").strip(),
            port=int(os.getenv("JOYTAG_INFER_PORT") or "8001"),
        )
