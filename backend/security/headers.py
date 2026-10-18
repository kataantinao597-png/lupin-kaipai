"""安全响应头中间件（Phase 9-1）。

- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY（防点击劫持）
- Referrer-Policy: strict-origin-when-cross-origin
- Permissions-Policy：禁用摄像头/麦克风/地理位置等敏感能力
- Content-Security-Policy-Report-Only：只记录不拦截，避免 breaking 现有前端；
  违规报告打到 /api/v1/security/csp-report（backend/api/v1/security.py）。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


CSP_REPORT_ONLY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "report-uri /api/v1/security/csp-report"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有响应加安全头。纯响应头，不改变业务行为。"""

    async def dispatch(self, request, call_next) -> Response:
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        # report-only：只上报不拦截，现有前端不受影响
        resp.headers["Content-Security-Policy-Report-Only"] = CSP_REPORT_ONLY
        return resp
