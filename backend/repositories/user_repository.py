"""用户 / refresh token / 密码重置的 Repository 实现。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth.models import PasswordReset, RefreshToken, User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, email: str, password_hash: str, display_name: str = "") -> User:
        user = User(email=email.lower().strip(), password_hash=password_hash, display_name=display_name)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email.lower().strip()))

    def get_by_id(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def set_password(self, user: User, password_hash: str) -> None:
        user.password_hash = password_hash
        self.db.commit()

    def deactivate(self, user: User) -> None:
        user.is_active = False
        self.db.commit()


class RefreshTokenRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id: str, token: str, expires_at: datetime) -> RefreshToken:
        row = RefreshToken(user_id=user_id, token_hash=_sha256_hex(token), expires_at=expires_at)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_valid(self, token: str) -> RefreshToken | None:
        row = self.db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _sha256_hex(token)))
        if row is None or row.revoked:
            return None
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= _utcnow():
            return None
        return row

    def revoke(self, row: RefreshToken) -> None:
        row.revoked = True
        self.db.commit()

    def revoke_all_for_user(self, user_id: str) -> int:
        rows = self.db.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)).all()
        for r in rows:
            r.revoked = True
        self.db.commit()
        return len(rows)


class PasswordResetRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id: str, token: str, expires_at: datetime) -> PasswordReset:
        row = PasswordReset(user_id=user_id, token_hash=_sha256_hex(token), expires_at=expires_at)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_valid(self, token: str) -> PasswordReset | None:
        row = self.db.scalar(select(PasswordReset).where(PasswordReset.token_hash == _sha256_hex(token)))
        if row is None or row.used:
            return None
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= _utcnow():
            return None
        return row

    def mark_used(self, row: PasswordReset) -> None:
        row.used = True
        self.db.commit()

    def count_recent_for_user(self, user_id: str, since: datetime) -> int:
        stmt = select(func.count()).select_from(PasswordReset).where(
            PasswordReset.user_id == user_id, PasswordReset.created_at >= since
        )
        return int(self.db.scalar(stmt) or 0)

    def recent_requests_for_email(self, email: str, within: timedelta) -> int:
        """同一邮箱 1 小时内请求次数（按用户聚合；不存在的用户返回 0，不泄露）。"""
        user = self.db.scalar(select(User).where(User.email == email.lower().strip()))
        if user is None:
            return 0
        return self.count_recent_for_user(user.id, _utcnow() - within)
