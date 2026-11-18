"""插件图片交付校验：只接受插件自己 ``data/`` 目录内的普通文件。

插件通过公开 API 替换影片封面、剧照或演员头像时，先把图片下载到自己的数据目录
（``<plugins_root>/<plugin_id>/data/...``），再把路径交给宿主。宿主只复制文件，
不删除、不移动插件目录里的任何内容，清理临时文件由插件自己负责。
"""

from pathlib import Path

from src.config.config import settings

# 单张图片文件上限：够覆盖 4K 原图，避免插件误交付超大文件拖垮导入链路。
MAX_PLUGIN_IMAGE_BYTES = 32 * 1024 * 1024


class PluginImageDeliveryError(ValueError):
    """插件交付的图片路径或文件不合法。"""


def resolve_plugin_image_path(plugin_id: str, raw_path: str | Path) -> Path:
    """校验并返回插件交付图片的绝对路径；越界、目录、超大文件都会被拒绝。"""
    if isinstance(raw_path, Path):
        raw_value = str(raw_path)
    elif isinstance(raw_path, str):
        raw_value = raw_path.strip()
    else:
        raise PluginImageDeliveryError("图片路径必须是字符串或 Path")
    if not raw_value:
        raise PluginImageDeliveryError("图片路径不能为空")
    data_root = (Path(settings.plugins.root_dir) / plugin_id / "data").resolve()
    try:
        path = Path(raw_value).expanduser().resolve()
    except OSError as exc:
        raise PluginImageDeliveryError("图片路径无法解析") from exc
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise PluginImageDeliveryError("图片必须是插件 data/ 目录内的普通文件") from exc
    if not path.is_file():
        raise PluginImageDeliveryError("图片文件不存在或不是普通文件")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PluginImageDeliveryError("图片文件无法读取") from exc
    if size > MAX_PLUGIN_IMAGE_BYTES:
        raise PluginImageDeliveryError("图片文件超过 32 MiB 上限")
    return path
