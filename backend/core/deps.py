"""023：依赖注入集中管理。

所有路由统一从这里 import 依赖，不再各处散写：

    from backend.core.deps import get_db, get_current_user, get_ai_service

- get_db：复用 backend/auth/database.py（SQLite 过渡 / PostgreSQL 生产）
- get_current_user：复用 backend/auth/deps.py（JWT 鉴权）
- get_ai_service：AI 服务单例 + 用户自带 Key（请求头 X-Lupin-Key）支持

依赖方向：api → core/deps → (auth, ai_service)，禁止反向依赖。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request

# 复用现有实现，不重复造轮子
from ..auth.database import get_db  # noqa: F401  (023: 数据库会话)
from ..auth.deps import get_current_user  # noqa: F401  (023: 当前用户)
from ..auth.models import User  # noqa: F401

from .config import get_settings
from .exceptions import AIConfigError_


# ---------- 限流（从 app.py 迁移） ----------

from collections import defaultdict
import time as _time

_rate_buckets: dict = defaultdict(list)
RATE_LIMIT = 20  # 每个 IP 每分钟最多 20 次 AI 请求
RATE_WINDOW = 60


def check_rate_limit(request: Request):
    """简单的内存限流。生产环境应换 Redis（TODO 039）。

    Phase 9-3 已废弃：分级限流由 RateLimitMiddleware 接管
    （backend/security/rate_limit.py），保留本函数仅为兼容旧导入。
    """
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    bucket = _rate_buckets[ip]
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    bucket.append(now)


def get_ai_service(request: Request):
    """023：AI 服务依赖。

    优先使用请求头中的用户自带配置（X-Lupin-Key / X-Lupin-Base，不持久化）；
    否则用服务端默认 AIService 单例；都没配置时抛 503（AIConfigError_）。
    """
    from ..ai_service import AIConfigError, AIService

    # 用户自带 Key（会话级，不写磁盘不记日志）
    user_key = request.headers.get("X-Lupin-Key", "").strip()
    if user_key:
        user_base = request.headers.get("X-Lupin-Base", "").strip() or None
        # Phase 9-2：用户自定义 Base URL 走 SSRF 校验
        if user_base:
            from ..security.ssrf import SSRFError, validate_outbound_url
            try:
                validate_outbound_url(user_base, purpose="自定义 API 地址")
            except SSRFError as e:
                raise HTTPException(status_code=400, detail=f"自定义 API 地址被拦截: {e}")
        try:
            return AIService(api_key=user_key, base_url=user_base)
        except AIConfigError as e:
            raise HTTPException(status_code=400, detail=f"自定义 API 配置无效: {e}")

    # 服务端单例（延迟初始化，兼容无 Key 启动）
    svc = getattr(get_ai_service, "_singleton", None)
    if svc is None:
        settings = get_settings()
        if not settings.LUPIN_API_KEY:
            raise AIConfigError_(
                "模型服务未配置：未找到可用的 API Key。"
                "请在设置中配置您的 API Key，或联系管理员配置服务默认 Key"
            )
        try:
            svc = AIService(
                api_key=settings.LUPIN_API_KEY,
                base_url=settings.LUPIN_API_BASE,
            )
        except AIConfigError as e:
            raise AIConfigError_(str(e))
        get_ai_service._singleton = svc
    return svc


def get_request_model(request: Request) -> Optional[str]:
    """从请求头获取用户指定的模型名称（X-Lupin-Model）。"""
    m = request.headers.get("X-Lupin-Model", "").strip()
    return m or None


def get_settings_dep():
    """FastAPI Depends 形式的配置注入。"""
    return get_settings()


__all__ = [
    "get_db",
    "get_current_user",
    "get_ai_service",
    "get_ai_gateway",
    "get_request_model",
    "get_settings_dep",
    "check_rate_limit",
    "User",
]


def get_ai_gateway(request: Request):
    """121：AI 网关依赖。包装 get_ai_service 的传输层。

    用法：gw=Depends(get_ai_gateway)，然后
    gw.generate_with_retry(task_type, make_messages, validate_fn, model=...)
    行为与原 svc.generate_with_retry 一致，但走网关（预算/重试/配置记录）。
    """
    from ..ai.gateway import AIGateway
    from ..auth.database import get_db as _get_db
    svc = get_ai_service(request)
    # org_id：从请求头 X-Org-Id 取（v1 新体系）；没有则 None（跳过预算）
    org_id = request.headers.get("X-Org-Id", "").strip() or None
    # db：有则做预算检查/扣减；拿不到也不阻断（降级为只记日志）
    db_gen = _get_db()
    try:
        db = next(db_gen)
    except StopIteration:
        db = None
    try:
        yield AIGateway(service=svc, org_id=org_id, db=db)
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
