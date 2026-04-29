from .actors import router as actors_router
from .movies import router as movies_router
from .tags import router as tags_router

__all__ = ["actors_router", "movies_router", "tags_router"]
