from src.common.image_payload import deobfuscate_image_bytes, looks_like_image


def _jpeg_payload() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01" + b"\x11" * 32


def _png_payload() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x22" * 32


def _webp_payload() -> bytes:
    return b"RIFF\x24\x00\x00\x00WEBP" + b"\x33" * 24


def _obfuscate(payload: bytes, key: int) -> bytes:
    """按 app CDN 的混淆方式造字节：首字节是 key，其余逐字节异或。"""
    return bytes([key]) + bytes(byte ^ key for byte in payload)


def test_looks_like_image_accepts_known_containers():
    assert looks_like_image(_jpeg_payload())
    assert looks_like_image(_png_payload())
    assert looks_like_image(_webp_payload())
    assert looks_like_image(b"GIF89a" + b"\x44" * 24)
    assert looks_like_image(b"\x00\x00\x00\x20ftypavif" + b"\x55" * 16)


def test_looks_like_image_rejects_non_images():
    assert not looks_like_image(b"<html>cdn error page</html>")
    assert not looks_like_image(b"\xff\xd8")
    assert not looks_like_image(b"")


def test_deobfuscate_restores_xor_payload_for_every_key():
    for payload in (_jpeg_payload(), _png_payload(), _webp_payload()):
        for key in (0x00, 0x56, 0xB6, 0xE1, 0xFF):
            assert deobfuscate_image_bytes(_obfuscate(payload, key)) == payload


def test_deobfuscate_keeps_plain_image_untouched():
    payload = _jpeg_payload()
    assert deobfuscate_image_bytes(payload) == payload


def test_deobfuscate_keeps_unknown_payload_untouched():
    payload = b"<html>cdn error page</html>"
    assert deobfuscate_image_bytes(payload) == payload


def test_deobfuscate_keeps_short_payload_untouched():
    payload = b"\x01\x02"
    assert deobfuscate_image_bytes(payload) == payload
