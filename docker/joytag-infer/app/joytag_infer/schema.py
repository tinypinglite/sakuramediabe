from typing import Any

from pydantic import BaseModel, Field


class RuntimeResource(BaseModel):
    backend: str
    execution_provider: str
    device: str
    device_full_name: str | None = None
    vector_size: int
    image_size: int
    model_name: str
    model_path: str
    available_providers: list[str] = Field(default_factory=list)
    probe_latency_ms: int | None = None


class EmbeddingItemResource(BaseModel):
    index: int
    ok: bool
    vector: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class EmbeddingBatchResource(BaseModel):
    items: list[EmbeddingItemResource]
