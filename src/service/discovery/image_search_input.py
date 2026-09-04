from io import BytesIO

from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError


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
