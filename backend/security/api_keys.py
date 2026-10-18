"""集成用 API Key（服务账号）签发与校验（Phase 9-4）。

- 明文格式：`lupin_` + secrets.token_urlsafe(32)（约 43 字符，256 bit 熵）
- 只存 SHA256 hex，绝不存明文；创建时只返回一次明文。
- 校验：hash → 查表 → 未撤销 → 未过期。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import models

_KEY_PREFIX = "lupin_"


def _now():
    return datetime.now(timezone.utc)


def generate_api_key() -> tuple[str, str]:
    """返回 (明文 key, sha256 hex)。明文只在此刻可见。"""
    plaintext = _KEY_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, digest


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def verify_api_key(db: Session, plaintext: str):
    """校验 X-API-Key。合法返回 ApiKey 记录；否则 None。"""
    if not plaintext or not plaintext.startswith(_KEY_PREFIX):
        return None
    rec = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.key_hash == hash_key(plaintext))
        .first()
    )
    if rec is None or rec.revoked_at is not None:
        return None
    if rec.expires_at is not None:
        exp = rec.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= _now():
            return None
    return rec


def touch_api_key(db: Session, rec) -> None:
    """更新 last_used_at（调用方负责 commit）。"""
    rec.last_used_at = _now()
    db.flush()
