"""S8.1：上传校验——魔数识别 + 大小限制。

不信任前端声明的 Content-Type：以后缀/声明 mime 做初步分类，
以文件头魔数为准判定真实类型，不一致直接拒绝。

大小限制（S8.1）：
- 图片 image/*：10MB
- 视频 video/*：200MB
- 压缩包 application/zip：50MB

零第三方依赖。
"""
from __future__ import annotations

# 类别 -> 大小上限（字节）
SIZE_LIMITS = {
    "image": 10 * 1024 * 1024,      # 10MB
    "video": 200 * 1024 * 1024,     # 200MB
    "archive": 50 * 1024 * 1024,    # 50MB
}

_MAX_SNIFF = 32


class MediaValidationError(ValueError):
    """校验失败：魔数不符 / 类型不支持 / 超大小。"""


def detect_mime(data: bytes) -> tuple[str, str] | None:
    """从文件头识别 (mime, category)。识别不出返回 None。

    支持：PNG / JPEG / GIF / WEBP / MP4(MOV) / ZIP。
    """
    head = data[:_MAX_SNIFF]
    if len(head) < 4:
        return None
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image/png", "image")
    # JPEG: FF D8 FF
    if head[:3] == b"\xff\xd8\xff":
        return ("image/jpeg", "image")
    # GIF: GIF87a / GIF89a
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ("image/gif", "image")
    # WEBP: RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ("image/webp", "image")
    # MP4/MOV: ftyp 在偏移 4
    if head[4:8] == b"ftyp":
        return ("video/mp4", "video")
    # ZIP: PK\x03\x04（空包 PK\x05\x06 / 分卷 PK\x07\x08 也放行，按 archive 计）
    if head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08"):
        return ("application/zip", "archive")
    return None


def category_of_mime(mime: str) -> str | None:
    """声明 mime -> 类别（用于上传意图阶段的预估大小限制）。"""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m in ("application/zip", "application/x-zip-compressed"):
        return "archive"
    return None


def validate_upload(data: bytes, declared_mime: str = "") -> tuple[str, str]:
    """完整校验：魔数识别 + 大小限制。

    返回 (真实 mime, 类别)。失败抛 MediaValidationError。
    声明 mime 与魔数类别不一致也拒绝（防改后缀）。
    """
    detected = detect_mime(data)
    if detected is None:
        raise MediaValidationError("不支持的文件类型（无法识别文件头）")
    mime, category = detected
    limit = SIZE_LIMITS[category]
    if len(data) > limit:
        raise MediaValidationError(
            f"文件过大：{len(data)} 字节，{category} 上限 {limit} 字节"
        )
    if declared_mime:
        declared_cat = category_of_mime(declared_mime)
        if declared_cat is not None and declared_cat != category:
            raise MediaValidationError(
                f"声明类型 {declared_mime} 与实际文件头不符（实为 {mime}）"
            )
    return mime, category
