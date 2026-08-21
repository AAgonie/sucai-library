"""Read-only image metadata helpers shared by local and SMB scans."""
from __future__ import annotations

import os
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def ratio_text(width: int, height: int) -> str:
    if not width or not height:
        return "未知"
    value = width / height
    if abs(value - 16 / 9) < 0.03:
        return "16:9"
    return f"{value:.2f}:1"


def read_image_size(path: Path) -> tuple[str, str]:
    """Return image dimensions without modifying the source file.

    Pillow supports every image format accepted by the scanner.  The compact
    PNG/JPEG parser remains as a fallback should Pillow be unavailable or fail
    to identify a valid image that exposes standard headers.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return f"{width} x {height}", ratio_text(width, height)
    except (ModuleNotFoundError, OSError, ValueError, SyntaxError):
        pass

    try:
        with path.open("rb") as file:
            header = file.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                width = int.from_bytes(header[16:20], "big")
                height = int.from_bytes(header[20:24], "big")
                return f"{width} x {height}", ratio_text(width, height)
            if header[:2] == b"\xff\xd8":
                file.seek(2)
                while True:
                    marker = file.read(2)
                    if len(marker) < 2:
                        break
                    while marker[0] != 0xFF:
                        marker = marker[1:] + file.read(1)
                    code = marker[1]
                    size_bytes = file.read(2)
                    if len(size_bytes) < 2:
                        break
                    size = int.from_bytes(size_bytes, "big")
                    if code in range(0xC0, 0xC4) or code in range(0xC5, 0xC8) or code in range(0xC9, 0xCC) or code in range(0xCD, 0xD0):
                        file.read(1)
                        height = int.from_bytes(file.read(2), "big")
                        width = int.from_bytes(file.read(2), "big")
                        if width > 0 and height > 0:
                            return f"{width} x {height}", ratio_text(width, height)
                        break
                    file.seek(size - 2, os.SEEK_CUR)
    except OSError:
        pass
    return "未知", "未知"
