"""Repository 层：认证相关的数据访问全部收敛在这里，路由层不写 SQL。

Phase 3 迁 PostgreSQL 时，只需替换这里的实现（或沿用 SQLAlchemy ORM），
路由与业务逻辑不需要改动。
"""
from .base import (  # noqa: F401
    EntityNotFound,
    OptimisticLockError,
    transactional,
    transactional_method,
)
from .fact_repository import FactRepository  # noqa: F401
from .storyboard_repository import StoryboardRepository  # noqa: F401
from .user_repository import (
    PasswordResetRepository,
    RefreshTokenRepository,
    UserRepository,
)
from .work_repository import WorkRepository  # noqa: F401

__all__ = ["UserRepository", "RefreshTokenRepository", "PasswordResetRepository"]
