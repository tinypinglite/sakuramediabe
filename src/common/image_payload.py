"""图片字节的形态识别，以及 JavDB app CDN 的单字节 XOR 还原。

JavDB 有两条图片链路，同一张图内容不同：

- app CDN（``tp.spfcas.com/<tenant>/...``）：**干净原图**，但字节被单字节 XOR
  混淆——首字节是本次响应的随机 key，其余字节逐字节异或它。PIL 直接读会失败，
  因此历史上被改写成网页 CDN。
- 网页 CDN（``c0.jdbstatic.com/...``）：正常 JPEG，但 ``covers/`` 带
  ``javdb.com`` 半透明水印，``samples/`` 与 app CDN 同源。

所以图片链路统一使用 app CDN，并在下载层做还原；识别只看魔数、不做解码，
避免为了"是不是图片"付出解码成本。
"""

from __future__ import annotations

from functools import lru_cache

# 宿主图片链路会遇到的容器格式。漏判不会破坏数据（不会对明文做异或），
# 只会让该格式的混淆字节无法还原，因此按实际出现过的格式收敛即可。
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_RIFF_MAGIC = b"RIFF"
_WEBP_TAG = b"WEBP"
_ISO_BMFF_TAG = b"ftyp"
_ISO_BMFF_BRANDS = (b"avif", b"avis", b"heic", b"heix", b"heif", b"mif1", b"msf1")

# 混淆字节的最小长度：短于此长度的载荷无法承载完整魔数，直接按非图片处理。
_MIN_IMAGE_BYTES = 12


def looks_like_image(data: bytes) -> bool:
    """按魔数判断字节是否像一张图片；不解析像素，不抛异常。"""
    if len(data) < _MIN_IMAGE_BYTES:
        return False
    if data.startswith((_JPEG_MAGIC, _PNG_MAGIC)):
        return True
    if data.startswith(_GIF_MAGICS):
        return True
    if data.startswith(_RIFF_MAGIC) and data[8:12] == _WEBP_TAG:
        return True
    return data[4:8] == _ISO_BMFF_TAG and data[8:12] in _ISO_BMFF_BRANDS


@lru_cache(maxsize=256)
def _xor_table(key: int) -> bytes:
    """256 字节异或表；bytes.translate 是 C 实现，比逐字节推导快两个量级。"""
    return bytes(byte ^ key for byte in range(256))


def deobfuscate_image_bytes(data: bytes) -> bytes:
    """还原 app CDN 的单字节 XOR 混淆；本来就不是混淆字节时原样返回。

    判定顺序保证不会误伤明文：先看原字节是不是图片，不是才尝试
    ``data[1:] ^ data[0]``，还原结果仍不是图片时返回原字节，让下游的
    解码校验报出真实错误，而不是把损坏数据写进图片库。
    """
    if looks_like_image(data):
        return data
    if len(data) < _MIN_IMAGE_BYTES:
        return data
    candidate = data[1:].translate(_xor_table(data[0]))
    if looks_like_image(candidate):
        return candidate
    return data
