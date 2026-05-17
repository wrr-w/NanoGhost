import base64
from typing import Optional


def image2base64(image_bytes: bytes, ext: str = "png") -> Optional[str]:
    """将图片 bytes 转为 data:image/...;base64 字符串。"""
    if not image_bytes:
        return None
    ext = ext.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    mime = f"image/{ext}" if ext in ("png", "jpeg", "gif", "webp") else "image/png"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"
