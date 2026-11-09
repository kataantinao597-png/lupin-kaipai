"""分级限流中间件（Phase 9-3）。

替代 deps.check_rate_limit（单层 20/min/IP），按路由分级：

- auth 档：登录/注册/密码重置 —— 5 次/分钟/IP（防撞库）
- ai 档：AI 生成类接口 —— 20 次/分钟/用户（按 IP + 鉴权头哈希区分用户）
- default 档：其余 /api/* —— 100 次/分钟/IP

超限返回 429 并带 Retry-After 头；/api/v1/* 走标准错误信封，
老 /api/* 保持 {"detail"} 格式。

内存实现（与 Phase 5 熔断/缓存、Phase 8 指标一致：多实例部署换 Redis）。
滑动窗口 + 线程锁。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..core import error_codes
from ..core.response import error as error_envelope

# (档名, 每分钟上限, 匹配路径片段)
_TIERS: list[tuple[str, int, tuple[str, ...]]] = [
    ("auth", 5, ("/auth/login", "/auth/register", "/auth/password-reset")),
    ("ai", 20, (
        "/api/ideas", "/api/storyboard", "/api/regenerate-shot",
        "/api/risk-check", "/api/feasibility-check", "/api/validate",
        "/api/v1/ai-tasks", "/api/v1/ai/", "/api/v1/agent",
    )),
    ("default", 100, ("/api/",)),
]

#: M-6：GET 轮询/状态/quota 单设轮询档（60/min/身份）。此前 ai 档只拦
#: POST 后，GET 轮询全落 default 档（100/min/IP），配合 S-1/S-2 可做
#: 任务存在性高频枚举与 DB 读放大。正常面板 2.5s 轮询约 24/min，
#: 60 留有余量；POST 建任务仍走 ai 档（20/min）。
_POLL_LIMIT = 60
_POLL_FRAGMENTS = (
    "/api/v1/ai-tasks",
    "/api/v1/agent/quota",
    "/api/v1/poster/image/quota",
    "/api/v1/poster/image/jobs",
)

# 不限流的路径（健康检查/指标/静态/文档/CSP 上报）
_SKIP_PREFIXES = (
    "/api/health", "/api/ready", "/api/v1/metrics",
    "/api/v1/security/csp-report",
    "/static", "/docs", "/openapi.json", "/favicon.ico", "/redoc",
)

_WINDOW = 60.0

#: 存活的中间件实例（仅测试隔离用：reset_rate_limit_buckets 清空所有桶）
_instances: list = []


def _tier_for(path: str, method: str = "GET") -> tuple[str, int] | None:
    for skip in _SKIP_PREFIXES:
        if path == skip or path.startswith(skip + "/") or path.startswith(skip + "?"):
            return None
    if path == "/":
        return None
    # M-6：GET 轮询先于 ai/default 档匹配（POST 不进轮询档，继续走 ai 档）
    if method.upper() == "GET":
        for frag in _POLL_FRAGMENTS:
            if frag in path:
                return "poll", _POLL_LIMIT
    for name, limit, fragments in _TIERS:
        for frag in fragments:
            if frag in path:
                # V3.2：ai 档只拦生成类写请求（POST）。GET 轮询/状态查询走
                # default 档——否则 Agent 面板 2.5s 轮询自身就会打满 20/min，
                # 把用户后续的发送/取消请求一起挤挂（表现为"点取消没反应"）。
                if name == "ai" and method.upper() != "POST":
                    break
                return name, limit
    return None


def _real_ip(request) -> str:
    """真实客户端 IP：优先 X-Forwarded-For 首跳（生产网关后 client.host
    只是网关出口 IP），无 XFF 时回退直连 client.host。

    只做身份分桶用，不做鉴权。注意：XFF 可由直连客户端伪造，故 auth
    档（防撞库）不走本函数，仍用直连 IP；ai/default 档伪造 XFF 只能
    让伪造者自己换桶，占不到别人的桶。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first and len(first) <= 64 and " " not in first:
            return first
    return request.client.host if request.client else "unknown"


def _identity_for(request, tier: str) -> str:
    ip = _real_ip(request) if tier != "auth" else (
        request.client.host if request.client else "unknown")
    if tier == "auth":
        return f"auth:{ip}"
    # ai/default：IP + 鉴权身份哈希，同一 IP 下不同用户分桶。
    # B-02：生产网关剥离 Authorization 时回退 lupin_refresh Cookie
    # （与 backend/auth/deps.py 的回退同名；此处只取哈希做分桶标识，
    # 不校验、不鉴权——鉴权仍在依赖层做）。
    authz = (request.headers.get("authorization", "")
             or request.headers.get("x-api-key", ""))
    if authz:
        who = "hdr:" + hashlib.sha256(authz.encode()).hexdigest()[:12]
    else:
        cookies = getattr(request, "cookies", None) or {}
        cookie = cookies.get("lupin_refresh", "")
        who = ("ckie:" + hashlib.sha256(cookie.encode()).hexdigest()[:12]
               if cookie else "anon")
    return f"{tier}:{ip}:{who}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """分级限流。桶：(档, 身份) -> deque[时间戳]。"""

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, deque] = {}
        self._lock = threading.Lock()
        _instances.append(self)

    def _allow(self, key: str, limit: int) -> tuple[bool, int]:
        """返回 (是否放行, 还需等待秒数)。仅测试与内部用。"""
        now = time.time()
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and now - bucket[0] >= _WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(_WINDOW - (now - bucket[0])) + 1
                return False, max(retry_after, 1)
            bucket.append(now)
            return True, 0

    def _too_many(self, request, retry_after: int) -> JSONResponse:
        headers = {"Retry-After": str(retry_after)}
        if request.url.path.startswith("/api/v1"):
            content = error_envelope(
                error_codes.AI_RATE_LIMITED,
                f"请求过于频繁，请 {retry_after} 秒后再试")
        else:
            content = {"detail": f"请求过于频繁，请 {retry_after} 秒后再试"}
        return JSONResponse(status_code=429, content=content,
                            headers=headers)

    async def dispatch(self, request, call_next):
        tier = _tier_for(request.url.path, request.method)
        if tier is None:
            return await call_next(request)
        name, limit = tier
        ok, retry_after = self._allow(_identity_for(request, name), limit)
        if not ok:
            return self._too_many(request, retry_after)
        return await call_next(request)


def reset_rate_limit_buckets() -> None:
    """清空所有限流桶（仅测试隔离用）。"""
    for inst in _instances:
        with inst._lock:
            inst._buckets.clear()
