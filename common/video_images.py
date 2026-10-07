"""Decode current YOLO VARBINARY images and legacy snapshot encodings."""
from __future__ import annotations

import base64
import binascii
from typing import Any, Optional, Tuple


def _image_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode(value: Any) -> Optional[Tuple[bytes, str]]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        mime = _image_type(data)
        if mime:
            return data, mime
        try:
            value = data.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\/", "/")
    if text.startswith("data:"):
        header, separator, text = text.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            return None
    text = "".join(text.split())
    try:
        if text.lower().startswith(("0x", "\\x")):
            data = bytes.fromhex(text[2:])
        else:
            data = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    mime = _image_type(data)
    return (data, mime) if mime else None


def decode_video_image(image_data: Any, legacy: Any) -> Optional[Tuple[bytes, str]]:
    """Prefer ImageData, falling back to ImageBase64 when it is not an image.

    The legacy field may contain JPEG bytes despite its name, or Base64 text
    returned by an ODBC driver as either str or bytes. MIME follows the bytes,
    since historical ImageFormat values are not always reliable.
    """
    return _decode(image_data) or _decode(legacy)
