"""鲁品开拍 - 用户认证模块（Phase 2，P0 041-044）。

提供注册/登录/退出/注销、JWT 会话、密码安全存储、密码重置。
数据层走 backend/repositories/，路由里不写 SQL。
"""
from .deps import get_current_user
from .models import User
from .router import router

# 供其他业务模块使用的类型别名，用法：user: CurrentUser = Depends(get_current_user)
CurrentUser = User

__all__ = ["router", "get_current_user", "CurrentUser", "User"]
