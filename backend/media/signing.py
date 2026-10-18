"""S8.1：短期签名下载 URL（HMAC-SHA256）。

本地存储没有 S3 那样的预签名能力：/assets/{id}/download（需登录）
返回 5 分钟有效的签名 URL，/assets/{id}/raw 验签后直接发文件，
不暴露永久路径、不要求登录（签名即凭证，注意保密传输）。

密钥：LUPIN_MEDIA_SIGN_SECRET；未配置则进程内随机生成并记警告
（开发可用，生产必须配置，否则重启后旧签名全部失效）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

log = logging.getLogger("lupin.media.signing")

SIGN_TTL_SECONDS = 300  # 5 分钟

_secret: str | None = None


def get_sign_secret() -> str:
    global _secret
    if _secret is None:
        s = os.environ.get("LUPIN_MEDIA_SIGN_SECRET", "").strip()
        if not s:
            s = secrets.token_hex(32)
            log.warning(
                "LUPIN_MEDIA_SIGN_SECRET 未配置，已生成进程内临时密钥；"
                "生产环境请配置固定值，否则重启后签名 URL 全部失效。"
            )
        _secret = s
    return _secret


def reset_sign_secret() -> None:
    """测试用：重置密钥单例。"""
    global _secret
    _secret = None


def sign(asset_id: str, exp: int) -> str:
    """生成签名。msg = f"{asset_id}:{exp}"。"""
    msg = f"{asset_id}:{exp}".encode("utf-8")
    return hmac.new(get_sign_secret().encode("utf-8"), msg,
                    hashlib.sha256).hexdigest()


def make_signed_params(asset_id: str, ttl: int = SIGN_TTL_SECONDS) -> dict:
    exp = int(time.time()) + ttl
    return {"exp": exp, "sig": sign(asset_id, exp)}


def verify(asset_id: str, exp: int, sig: str) -> bool:
    """验签：签名正确且未过期。"""
    if not sig or exp <= int(time.time()):
        return False
    return hmac.compare_digest(sign(asset_id, exp), sig)
