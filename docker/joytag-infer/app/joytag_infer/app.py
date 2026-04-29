from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile

from joytag_infer.runtime import JoyTagOnnxRuntime
from joytag_infer.schema import (
    EmbeddingBatchResource,
    EmbeddingItemResource,
    RuntimeResource,
)
from joytag_infer.settings import JoyTagInferSettings


def _create_auth_dependency(settings: JoyTagInferSettings):
    def require_api_key(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not settings.api_key:
            return
        expected = f"Bearer {settings.api_key}"
        if authorization != expected:
            raise HTTPException(
                status_code=401,
                detail={
                    "error_code": "unauthorized",
                    "message": "Invalid inference service token",
                },
            )

    return require_api_key


def create_app(
    *,
    runtime: JoyTagOnnxRuntime | None = None,
    settings: JoyTagInferSettings | None = None,
) -> FastAPI:
    infer_settings = settings or JoyTagInferSettings.from_env()
    infer_runtime = runtime or JoyTagOnnxRuntime(infer_settings)
    require_api_key = _create_auth_dependency(infer_settings)
    app = FastAPI(title="joytag-infer", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz(_: None = Depends(require_api_key)):
        return {
            "ok": True,
            "backend": infer_runtime.backend,
            "model_name": infer_runtime.model_name,
        }

    @app.get("/v1/runtime", response_model=RuntimeResource)
    def get_runtime(_: None = Depends(require_api_key)):
        try:
            return RuntimeResource.model_validate(infer_runtime.runtime_info(probe=True))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "runtime_probe_failed",
                    "message": str(exc),
                },
            ) from exc

    @app.post("/v1/embeddings/images", response_model=EmbeddingBatchResource)
    async def embed_images(
        files: Annotated[list[UploadFile], File(...)],
        _: None = Depends(require_api_key),
    ):
        if not files:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "empty_batch",
                    "message": "No input files provided",
                },
            )

        valid_payloads: list[bytes] = []
        valid_indexes: list[int] = []
        items: list[EmbeddingItemResource | None] = [None] * len(files)

        for index, upload in enumerate(files):
            payload = await upload.read()
            if not payload:
                items[index] = EmbeddingItemResource(
                    index=index,
                    ok=False,
                    error_code="empty_image",
                    error_message="Image file is empty",
                )
                continue
            valid_payloads.append(payload)
            valid_indexes.append(index)

        if valid_payloads:
            try:
                vectors = infer_runtime.embed_image_batch(valid_payloads)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_code": "invalid_image",
                        "message": str(exc),
                    },
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error_code": "inference_failed",
                        "message": str(exc),
                    },
                ) from exc
            if len(vectors) != len(valid_indexes):
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error_code": "invalid_batch_result",
                        "message": "Inference batch result count mismatch",
                    },
                )
            for index, vector in zip(valid_indexes, vectors):
                items[index] = EmbeddingItemResource(
                    index=index,
                    ok=True,
                    vector=[float(value) for value in vector],
                    metadata={
                        "provider": "joytag",
                        "backend": infer_runtime.backend,
                        "execution_provider": infer_runtime.execution_provider,
                        "device": infer_runtime.device,
                        "image_size": infer_runtime.image_size,
                        "vector_size": infer_runtime.vector_size,
                    },
                )

        normalized_items = [
            item
            if item is not None
            else EmbeddingItemResource(
                index=index,
                ok=False,
                error_code="invalid_batch_item",
                error_message="Missing inference item",
            )
            for index, item in enumerate(items)
        ]
        return EmbeddingBatchResource(items=normalized_items)

    return app
