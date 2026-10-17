"""请求 ID：中间件生成/透传，全链路追踪用。

- 客户端可通过请求头 X-Request-ID 传入自己的 ID（便于联调）；
- 未传入时服务端生成 16 位随机 hex；
- 响应头 X-Request-ID 原样返回；
- 统一错误响应里的 request_id 与之一致。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_request_id_var: ContextVar[str] = ContextVar("lupin_request_id", default="")


def get_request_id() -> str:
    """取当前请求的 request_id；上下文外调用时返回空字符串。"""
    return _request_id_var.get()


def set_request_id(value: str) -> None:
    _request_id_var.set(value)


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:16]
    set_request_id(rid)
    return rid


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = (request.headers.get("X-Request-ID") or "").strip()[:64]
        if not rid:
            rid = uuid.uuid4().hex[:16]
        token = _request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response
