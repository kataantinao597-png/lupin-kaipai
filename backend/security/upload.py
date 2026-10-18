"""上传文件安全校验（Phase 9-6）。

原则：
1. 只认文件魔数（magic number），不认扩展名——扩展名可随意伪造。
2. 大小按类型分级：图片 10MB、视频 200MB、压缩包 50MB。
3. 未知类型直接拒绝（fail closed）。

当前项目尚无上传接口，本模块作为统一校验入口供后续上传功能使用。
"""
from __future__ import annotations


class UploadRejected(ValueError):
    """上传被拒绝：类型未知或超限。"""


# kind -> [(offset, magic bytes), ...]
_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "image": [
        (0, b"\x89PNG\r\n\x1a\n"),   # PNG
        (0, b"\xff\xd8\xff"),        # JPEG
        (0, b"GIF87a"),              # GIF87a
        (0, b"GIF89a"),              # GIF89a
        (0, b"RIFF"),                # WebP（需 offset 8 校验 "WEBP"）
        (0, b"BM"),                  # BMP
    ],
    "video": [
        (4, b"ftyp"),                # MP4/MOV（box size 之后）
        (0, b"\x1a\x45\xdf\xa3"),    # WebM/MKV (EBML)
        (0, b"FLV"),                 # FLV
    ],
    "archive": [
        (0, b"PK\x03\x04"),          # ZIP
        (0, b"PK\x05\x06"),          # ZIP（空包）
        (0, b"PK\x07\x08"),          # ZIP（分卷）
        (0, b"Rar!\x1a\x07\x00"),    # RAR
        (0, b"\x1f\x8b"),            # GZIP
    ],
}

SIZE_LIMITS: dict[str, int] = {
    "image": 10 * 1024 * 1024,      # 10MB
    "video": 200 * 1024 * 1024,     # 200MB
    "archive": 50 * 1024 * 1024,    # 50MB
}

_MAX_MAGIC_OFFSET = 8


def sniff_kind(head: bytes) -> str | None:
    """从文件头识别类型；未知返回 None。"""
    if not head:
        return None
    for kind, sigs in _MAGIC.items():
        for offset, magic in sigs:
            if len(head) >= offset + len(magic) and \
                    head[offset:offset + len(magic)] == magic:
                # WebP 需二次确认
                if magic == b"RIFF":
                    if len(head) >= 12 and head[8:12] == b"WEBP":
                        return kind
                    continue
                return kind
    return None


def validate_upload(filename: str, head: bytes, size: int) -> str:
    """校验上传文件，返回识别到的类型（image/video/archive）。

    filename 仅用于错误信息，不参与类型判定。
    head 为文件前至少 12 字节；size 为声明的文件大小（字节）。
    未知类型或超限抛 UploadRejected。
    """
    kind = sniff_kind(head)
    if kind is None:
        raise UploadRejected(
            f"文件 {filename!r} 类型无法识别（魔数校验失败），已拒绝")
    limit = SIZE_LIMITS[kind]
    if size > limit:
        raise UploadRejected(
            f"文件 {filename!r} 过大：{kind} 类型上限 "
            f"{limit // 1024 // 1024}MB，实际 {size} 字节")
    if size <= 0:
        raise UploadRejected(f"文件 {filename!r} 为空，已拒绝")
    return kind
