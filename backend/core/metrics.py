"""S9.3：内存指标聚合。

不引入 Prometheus client（Phase 8 约定），用线程安全的内存结构聚合：
- API 请求量/成功率/P95 延迟（按路由模板聚合）
- 数据库错误率
- AI 超时/重试计数

/db 侧的 token、费用、任务状态走 ai_calls/ai_tasks 表实时查，
这里只聚合内存里便宜的东西。
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict, deque

_LOCK = threading.Lock()

# 路由级请求统计：route -> {"total": n, "ok": n(2xx/3xx), "lat": deque(ms, 最多2000)}
_route_stats: dict[str, dict] = defaultdict(
    lambda: {"total": 0, "ok": 0, "lat": deque(maxlen=2000)}
)

# 数据库调用统计
_db_total = 0
_db_errors = 0

# AI 超时/重试计数
_ai_timeouts = 0
_ai_retries: dict[str, int] = defaultdict(int)  # category -> count

# ID 归一化：UUID / 纯数字 / 32位hex 替换为占位符，避免路由爆炸
_ID_RE = re.compile(
    r"/(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}|\d+)"
)


def normalize_route(path: str) -> str:
    """把 /api/v1/works/abc123 归一化为 /api/v1/works/{id}。"""
    return _ID_RE.sub("/{id}", path)


def record_request(route: str, status_code: int, latency_ms: float) -> None:
    route = normalize_route(route)
    with _LOCK:
        s = _route_stats[route]
        s["total"] += 1
        if 200 <= status_code < 400:
            s["ok"] += 1
        s["lat"].append(latency_ms)


def record_db(ok: bool) -> None:
    global _db_total, _db_errors
    with _LOCK:
        _db_total += 1
        if not ok:
            _db_errors += 1


def record_ai_timeout() -> None:
    global _ai_timeouts
    with _LOCK:
        _ai_timeouts += 1


def record_ai_retry(category: str) -> None:
    with _LOCK:
        _ai_retries[category] += 1


def _p95(samples) -> float:
    samples = sorted(samples)
    if not samples:
        return 0.0
    idx = min(len(samples) - 1, int(len(samples) * 0.95))
    return round(samples[idx], 1)


def snapshot() -> dict:
    """返回当前聚合快照，供 /api/v1/metrics 用。"""
    with _LOCK:
        routes = {}
        total, ok = 0, 0
        for route, s in _route_stats.items():
            t, o = s["total"], s["ok"]
            total += t
            ok += o
            routes[route] = {
                "requests": t,
                "success_rate": round(o / t, 4) if t else 0.0,
                "p95_latency_ms": _p95(s["lat"]),
            }
        return {
            "api": {
                "total_requests": total,
                "success_rate": round(ok / total, 4) if total else 1.0,
                "routes": routes,
            },
            "db": {
                "total": _db_total,
                "errors": _db_errors,
                "error_rate": round(_db_errors / _db_total, 4) if _db_total else 0.0,
            },
            "ai": {
                "timeouts": _ai_timeouts,
                "retries": dict(_ai_retries),
            },
        }


def reset() -> None:
    """测试用：清空聚合。"""
    global _db_total, _db_errors, _ai_timeouts
    with _LOCK:
        _route_stats.clear()
        _db_total = 0
        _db_errors = 0
        _ai_timeouts = 0
        _ai_retries.clear()


# ---------- 请求指标中间件 ----------

# 静态资源/健康检查不计入业务指标，避免噪音
_SKIP_PREFIXES = ("/static/", "/favicon.ico")


class MetricsMiddleware:
    """BaseHTTPMiddleware 风格的指标中间件。

    挂在 RequestIDMiddleware 内侧（后 add），保证 request_id 已就绪。
    每个请求记录：路由、状态码、延迟 → 内存聚合；并打一条结构化
    access 日志（含 S9.3 全部字段）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import time as _time

        from starlette.requests import Request
        from starlette.responses import Response

        request = Request(scope, receive)
        path = request.url.path
        start = _time.time()
        status_code = 500
        try:
            async def _send_wrapper(message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)

            await self.app(scope, receive, _send_wrapper)
        finally:
            latency_ms = round((_time.time() - start) * 1000, 1)
            if not path.startswith(_SKIP_PREFIXES) and path != "/api/health":
                record_request(path, status_code, latency_ms)
                # 结构化 access 日志（S9.3 字段集）
                try:
                    from .logging import get_logger as _gl
                    from .request_id import get_request_id as _grid
                    _gl("access").info(
                        f"{request.method} {path} -> {status_code} "
                        f"{latency_ms}ms",
                        extra={
                            "request_id": _grid() or None,
                            "route": normalize_route(path),
                            "status_code": status_code,
                            "latency_ms": latency_ms,
                            "service": "lupin-kaipai",
                        },
                    )
                except Exception:
                    pass
