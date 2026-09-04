"""宿主扩展点目录：key -> 领域校验器。

核心机制（loader）只做通用结构校验；本目录登记宿主支持的扩展点，
并把 data 的领域解释委托给对应领域模块。新增业务扩展点时在这里注册，
机制本身不需要改动。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.plugins.extensions.media_provider import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    validate_media_provider_extension,
)
from src.plugins.extensions.metadata import (
    METADATA_SOURCE_EXTENSION_KEY,
    validate_metadata_extension,
)
from src.plugins.extensions.ranking import (
    RANKING_SOURCE_EXTENSION_KEY,
    validate_ranking_extension,
)

# key -> validator(plugin_id, extension)；未登记的 key 会被 loader 拒绝。
EXTENSION_VALIDATORS: dict[str, Callable[..., Any]] = {
    METADATA_SOURCE_EXTENSION_KEY: validate_metadata_extension,
    RANKING_SOURCE_EXTENSION_KEY: validate_ranking_extension,
    MEDIA_PROVIDER_EXTENSION_KEY: validate_media_provider_extension,
}
