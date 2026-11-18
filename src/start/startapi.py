

import uvicorn

from src.common.logging import configure_logging, get_logging_level_name

if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
        log_level=get_logging_level_name().lower(),
    )
