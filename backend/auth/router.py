"""认证路由：/api/v1/auth。

端点：
  POST   /register         注册
  POST   /login            登录（返回 access token，refresh token 进 httpOnly Cookie）
  POST   /refresh          用 Cookie 中的 refresh token 换新的 access token（轮换）
  POST   /logout           退出（吊销当前 refresh token，清 Cookie）
  GET    /me               当前登录用户信息（需 Bearer token）
  DELETE /account           注销账号（软删除 + 吊销全部 refresh token，需 Bearer token）
  POST   /forgot-password  申请密码重置（限流：同一邮箱 1 小时最多 3 次）
  POST   /reset-password   用一次性 token 重置密码

注意：重置 token 的投递（邮件/短信）在 Phase 3 接入；调试时可设
LUPIN_AUTH_DEBUG=1 让 forgot-password 在响应中返回 token，仅用于开发联调。
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..repositories.user_repository import (
    PasswordResetRepository,
    RefreshTokenRepository,
    UserRepository,
)
from .config import (
    FORGOT_RATE_LIMIT_PER_HOUR,
    PASSWORD_MIN_LENGTH,
    REFRESH_TOKEN_EXPIRE_DAYS,
    RESET_TOKEN_EXPIRE_MINUTES,
    hash_password,
    verify_password,
)
from .database import get_db
from .deps import get_current_user
from .models import User
from .security import create_access_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = "lupin_refresh"
_AUTH_DEBUG = os.environ.get("LUPIN_AUTH_DEBUG") == "1"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Schemas ----------


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)
    display_name: str = Field(default="", max_length=100)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    created_at: datetime
    is_active: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user: UserOut


def _user_out(u: User) -> UserOut:
    return UserOut(id=u.id, email=u.email, display_name=u.display_name, created_at=u.created_at, is_active=u.is_active)


def _check_email(email: str) -> str:
    email = email.strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="邮箱格式不正确")
    return email


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/api/v1/auth",
        # 生产 HTTPS 环境应为 True；本地开发保持 False 以免 Cookie 被浏览器拒绝
        secure=os.environ.get("LUPIN_COOKIE_SECURE") == "1",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


def _issue_session(db: Session, response: Response, user: User) -> TokenOut:
    from .config import ACCESS_TOKEN_EXPIRE_MINUTES

    access_token, _ = create_access_token(user.id)
    refresh_token = secrets.token_urlsafe(32)
    RefreshTokenRepository(db).create(
        user.id, refresh_token, _utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    )
    _set_refresh_cookie(response, refresh_token)
    return TokenOut(
        access_token=access_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=_user_out(user),
    )


# ---------- 端点 ----------


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    email = _check_email(data.email)
    repo = UserRepository(db)
    if repo.get_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    try:
        user = repo.create(email, hash_password(data.password), data.display_name.strip())
    except IntegrityError:
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    return _user_out(user)


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, response: Response, db: Session = Depends(get_db)):
    repo = UserRepository(db)
    user = repo.get_by_email(data.email.strip())
    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码不正确")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已注销")
    return _issue_session(db, response, user)


@router.post("/refresh", response_model=TokenOut)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 refresh token")
    repo = RefreshTokenRepository(db)
    row = repo.get_valid(token)
    if row is None:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效或已过期")
    user = UserRepository(db).get_by_id(row.user_id)
    if user is None or not user.is_active:
        repo.revoke(row)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    # 轮换：吊销旧 token，签发新一对
    repo.revoke(row)
    return _issue_session(db, response, user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        row = RefreshTokenRepository(db).get_valid(token)
        if row:
            RefreshTokenRepository(db).revoke(row)
    _clear_refresh_cookie(response)
    return {"message": "已退出登录"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _user_out(user)


@router.delete("/account")
def delete_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    RefreshTokenRepository(db).revoke_all_for_user(user.id)
    UserRepository(db).deactivate(user)
    return {"message": "账号已注销"}


@router.post("/forgot-password")
def forgot_password(data: ForgotIn, db: Session = Depends(get_db)):
    email = data.email.strip()
    # 统一返回，不泄露邮箱是否注册
    result: dict = {"message": "如果该邮箱已注册，重置链接已发送，请查收"}
    user = UserRepository(db).get_by_email(email) if _EMAIL_RE.match(email) else None
    if user is not None and user.is_active:
        repo = PasswordResetRepository(db)
        recent = repo.recent_requests_for_email(email, timedelta(hours=1))
        if recent >= FORGOT_RATE_LIMIT_PER_HOUR:
            raise HTTPException(status_code=429, detail="请求过于频繁，请 1 小时后再试")
        token = secrets.token_urlsafe(32)
        repo.create(user.id, token, _utcnow() + timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES))
        # TODO(Phase 3)：通过邮件/短信投递 token
        if _AUTH_DEBUG:
            result["reset_token"] = token
    return result


@router.post("/reset-password")
def reset_password(data: ResetIn, db: Session = Depends(get_db)):
    repo = PasswordResetRepository(db)
    row = repo.get_valid(data.token)
    if row is None:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    user_repo = UserRepository(db)
    user = user_repo.get_by_id(row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    user_repo.set_password(user, hash_password(data.new_password))
    repo.mark_used(row)
    # 密码重置后吊销全部会话，强制重新登录
    RefreshTokenRepository(db).revoke_all_for_user(user.id)
    return {"message": "密码已重置，请重新登录"}
