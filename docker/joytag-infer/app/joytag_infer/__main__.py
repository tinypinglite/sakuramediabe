import logging

import uvicorn

from joytag_infer.app import create_app
from joytag_infer.settings import JoyTagInferSettings


if __name__ == "__main__":
    # uvicorn 只配置 uvicorn.* 三个 logger，不碰 root；不在这里装 handler 的话，
    # joytag_infer.* 的 info 日志会被直接丢弃，warning 也只能走无时间戳的 lastResort。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings = JoyTagInferSettings.from_env()
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
