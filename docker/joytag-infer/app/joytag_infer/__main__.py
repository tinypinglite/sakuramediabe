import uvicorn

from joytag_infer.app import create_app
from joytag_infer.settings import JoyTagInferSettings


if __name__ == "__main__":
    settings = JoyTagInferSettings.from_env()
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
