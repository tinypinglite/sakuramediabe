from io import BytesIO

import pillow_heif
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError

# 让 Pillow 直接解码 iOS 相册上传的 HEIC/HEIF。
pillow_heif.register_heif_opener()


def normalize_image_search_query(image_bytes: bytes) -> bytes:
    try:
        with PillowImage.open(BytesIO(image_bytes)) as image:
            image.seek(0)
            image.load()
            normalized = ImageOps.exif_transpose(image)
            if normalized.mode not in {"RGB", "RGBA"}:
                normalized = normalized.convert(
                    "RGBA" if "transparency" in normalized.info else "RGB"
                )
            output = BytesIO()
            normalized.save(output, format="WEBP", lossless=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("uploaded image is invalid or unsupported") from exc
