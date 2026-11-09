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


#: M-4：自带 Key 可信域名白名单（与前端 TRUSTED_API_HOSTS 同列表，
#: 精确匹配或子域名后缀，大小写不敏感）。
TRUSTED_API_HOSTS = ("deepkey.top", "api.openai.com")


def _api_host_allowed(base: str) -> bool:
    """base URL 的 hostname 是否在可信列表内（前端 _apiHostAllowed 同款）。"""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(base).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d)
               for d in TRUSTED_API_HOSTS)


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
            # M-4：后端域名白名单（与前端 getApiConfig 的 TRUSTED_API_HOSTS
            # 同列表）。此前白名单只在前端生效，直接调 API 带任意公网域名
            # 即可让服务端把用户 Key 发往任意主机；现后端强制执行。
            # 轻微 #6：同时强制 https——http 明文会让 Key 裸奔。
            from urllib.parse import urlsplit
            try:
                _scheme = (urlsplit(user_base).scheme or "").lower()
            except ValueError:
                _scheme = ""
            if _scheme != "https":
                raise HTTPException(
                    status_code=400,
                    detail="自定义 API 地址必须使用 https（明文传输会泄露 Key）")
            if not _api_host_allowed(user_base):
                raise HTTPException(
                    status_code=400,
                    detail="自定义 API 地址域名不在可信列表内（仅支持 "
                           "deepkey.top、api.openai.com 及其子域名）")
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
    # org_id：优先请求头 X-Org-Id（v1 新体系），其次 ?org_id= 查询参数。
    # 生产网关会剥离自定义请求头，只留 Cookie；若无 query 兜底，预算会静默失效
    # （表现为"预算已生效"但实际一分钱都没记账）。两者都没有则 None → 跳过预算。
    org_id = (request.headers.get("X-Org-Id", "").strip()
              or (request.query_params.get("org_id") or "").strip()
              or None)
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
