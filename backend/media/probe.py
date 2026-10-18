"""S8.2：媒体元数据提取。

- 图片：直接解析文件头拿宽高（PNG/JPEG/GIF/WEBP），零第三方依赖。
- 视频：调 ffprobe（参数数组，禁止 shell 拼接），超时 60s；
  本机无 ffprobe 时标记 probe=unavailable，不阻断主流程。

返回 dict，直接存入 assets.meta_json / media_tasks.meta_json。
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess


# ---------------- 图片：文件头解析 ----------------

def _png_size(data: bytes) -> tuple[int, int] | None:
    # IHDR 在偏移 16：width(4) height(4)，大端
    if len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    # 逻辑屏幕宽高在偏移 6：小端 uint16
    if len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    # 扫描 marker 找 SOF0-SOF15（除 DHT/DAC）：结构 FF xx len(2) data
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        i += 2
        # standalone markers（无长度段）
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > n:
            break
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        if seg_len < 2 or i + seg_len > n:
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # SOF：precision(1) height(2) width(2)
            h, w = struct.unpack(">HH", data[i + 3:i + 7])
            return (w, h)
        i += seg_len
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        # 帧头 14 字节后：width(2, 低14位) height(2, 低14位)，小端
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return (w, h)
    if chunk == b"VP8L" and len(data) >= 25:
        # VP8L 头 4 字节：14位宽-1 + 14位高-1
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = 1 + (((b1 & 0x3F) << 8) | b0)
        h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return (w, h)
    if chunk == b"VP8X" and len(data) >= 30:
        # canvas 尺寸在偏移 24：3字节宽-1 + 3字节高-1，大端
        w = int.from_bytes(data[24:27], "big") + 1
        h = int.from_bytes(data[27:30], "big") + 1
        return (w, h)
    return None


def image_dimensions(data: bytes, mime: str) -> dict:
    """解析图片宽高。失败返回 {\"width\": None, ...}，不抛错。"""
    size = None
    try:
        if mime == "image/png":
            size = _png_size(data)
        elif mime == "image/jpeg":
            size = _jpeg_size(data)
        elif mime == "image/gif":
            size = _gif_size(data)
        elif mime == "image/webp":
            size = _webp_size(data)
    except Exception:  # noqa: BLE001 — 头解析失败只记 None
        size = None
    return {
        "kind": "image",
        "mime": mime,
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "bytes": len(data),
    }


# ---------------- 视频：ffprobe ----------------

_FFPROBE_TIMEOUT = 60  # S8.2：超时 60s


def video_probe(path: str) -> dict:
    """用 ffprobe 提取视频元数据（参数数组调用，无 shell 拼接）。

    path 为本地文件路径。无 ffprobe 时返回 probe=unavailable。
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"kind": "video", "probe": "unavailable",
                "reason": "ffprobe not installed"}
    # 参数数组：用户输入只出现在末尾的 path 位置，不拼 shell
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration,size:stream=width,height,codec_name,codec_type",
        "-of", "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFPROBE_TIMEOUT,
                              check=False)
    except subprocess.TimeoutExpired:
        return {"kind": "video", "probe": "failed", "reason": "ffprobe timeout"}
    except OSError as e:
        return {"kind": "video", "probe": "failed", "reason": str(e)}
    if proc.returncode != 0:
        return {"kind": "video", "probe": "failed",
                "reason": (proc.stderr.decode("utf-8", "replace")[:200]
                           or f"exit {proc.returncode}")}
    try:
        info = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"kind": "video", "probe": "failed",
                "reason": "ffprobe 输出非 JSON"}
    fmt = info.get("format", {}) or {}
    streams = info.get("streams", []) or []
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream and streams:
        video_stream = streams[0]
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") else None
    except (TypeError, ValueError):
        duration = None
    return {
        "kind": "video",
        "probe": "ok",
        "duration_seconds": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "bytes": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
    }


def extract_metadata(data: bytes, mime: str, local_path: str | None) -> dict:
    """统一入口：按 mime 分发。"""
    category = mime.split("/")[0] if "/" in mime else ""
    if category == "image":
        return image_dimensions(data, mime)
    if category == "video":
        if local_path:
            return video_probe(local_path)
        return {"kind": "video", "probe": "unavailable",
                "reason": "no local path"}
    return {"kind": category or "unknown", "mime": mime, "bytes": len(data)}
