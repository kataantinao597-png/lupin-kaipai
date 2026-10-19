"""认证依赖：从 Authorization: Bearer <token> 解析当前用户。

不允许前端自声明身份——所有需要登录的接口必须 Depends(get_current_user)。

部署注意：公网网关可能剥离 Authorization 头，此时回退校验 refresh Cookie
（lupin_refresh），保证登录态在生产环境可用。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..repositories.user_repository import RefreshTokenRepository, UserRepository
from .database import get_db
from .models import User
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

# 与 auth/router.py 的 REFRESH_COOKIE 同值；写死字面量避免循环导入
REFRESH_COOKIE_NAME = "lupin_refresh"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    request: Request = None,  # type: ignore[assignment]  # FastAPI 按类型注入 Request
    db: Session = Depends(get_db),
) -> User:
    user_id: str | None = None
    bearer_expired = False
    if credentials is not None and credentials.credentials:
        user_id = decode_access_token(credentials.credentials)
        bearer_expired = user_id is None
    if user_id is None and request is not None:
        # 回退：网关剥离 Authorization 头时，用 refresh Cookie 校验身份
        token = request.cookies.get(REFRESH_COOKIE_NAME)
        if token:
            row = RefreshTokenRepository(db).get_valid(token)
            if row is not None:
                user_id = row.user_id
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录" if bearer_expired else "未登录",
        )
    user = UserRepository(db).get_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    return user
