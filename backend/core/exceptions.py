"""021：统一异常体系。

业务异常、鉴权异常、校验异常、外部服务异常、系统异常分层，
API 层统一映射为 HTTP 状态码（见 core/deps.py 的异常处理器或各路由）。

现有代码中的 HTTPException 直接抛出保持兼容；
新 services 层优先抛这里的业务异常，由路由层转换。
"""
from __future__ import annotations


class LupinError(Exception):
    """所有业务异常的基类。携带稳定错误码，前端可按码展示。"""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str = "", *, code: str | None = None):
        self.message = message or self.code
        if code:
            self.code = code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ---------- 业务异常 ----------


class NotFoundError(LupinError):
    code = "NOT_FOUND"
    status_code = 404


class WorkNotFound(NotFoundError):
    code = "WORK_NOT_FOUND"


class FactNotFound(NotFoundError):
    code = "FACT_NOT_FOUND"


class ValidationError(LupinError):
    """业务规则校验失败（区别于 Pydantic 的请求结构校验）。"""

    code = "VALIDATION_FAILED"
    status_code = 400


class ConflictError(LupinError):
    """资源冲突（如乐观锁版本冲突）。"""

    code = "CONFLICT"
    status_code = 409


# ---------- 鉴权异常 ----------


class AuthError(LupinError):
    code = "UNAUTHORIZED"
    status_code = 401


class PermissionDenied(LupinError):
    code = "FORBIDDEN"
    status_code = 403


# ---------- 外部服务异常 ----------


class AIServiceError(LupinError):
    """AI 调用失败（超时、配额、格式错误等）。"""

    code = "AI_ERROR"
    status_code = 502


class AITimeout(AIServiceError):
    code = "AI_TIMEOUT"


class AIConfigError_(LupinError):
    """AI 未配置（无 Key）。对应 HTTP 503。"""

    code = "AI_NOT_CONFIGURED"
    status_code = 503


# ---------- 限流 ----------


class RateLimitExceeded(LupinError):
    code = "RATE_LIMITED"
    status_code = 429


def lupin_error_to_response(exc: LupinError) -> dict:
    """028 预留：统一错误响应结构 {code, message, request_id}。

    request_id 由中间件注入（TODO: 039 接入时补全）。
    """
    return {"code": exc.code, "message": exc.message}


# =====================================================================
# 026：API 层统一异常（int 业务码，见 core/error_codes.py）。
#
# 与上面的 LupinError（021，services 层，字符串码）并存：
# - 新 v1 接口优先抛这里的异常，由 app.py 全局 handler 转为标准错误响应；
# - 老 /api/* 接口继续抛 HTTPException，保持原有响应格式；
# - 全局 handler 同时兼容 LupinError（按其 status_code/code 转换）。
# =====================================================================
from typing import Any, Optional  # noqa: E402

from . import error_codes as _codes  # noqa: E402


class AppError(Exception):
    """API 层业务异常基类。

    code: int 业务错误码（error_codes）；http_status: HTTP 状态码；
    details: 可选调试细节（不得含密钥等敏感信息）。
    """

    http_status: int = 500
    code: int = _codes.SYSTEM_ERROR

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[int] = None,
        details: Any = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.message = message or _codes.message_for(self.code)
        self.details = details


class BusinessError(AppError):
    """业务规则违反 → HTTP 400。如：参数非法、状态非法、重复操作。"""

    http_status = 400
    code = _codes.INVALID_PARAM


class AuthError(AppError):
    """认证/授权失败。未登录 → 401；已登录但权限不足 → 403（forbidden=True）。"""

    http_status = 401
    code = _codes.UNAUTHENTICATED

    def __init__(self, message: str = "", *, forbidden: bool = False, **kwargs: Any) -> None:
        if forbidden and "code" not in kwargs:
            kwargs["code"] = _codes.PERMISSION_DENIED
        super().__init__(message, **kwargs)
        if forbidden:
            self.http_status = 403


class ValidationError(AppError):
    """026 规范定义：业务层数据校验失败 → HTTP 422。

    注意：本模块 021 区另有一个同名 LupinError 子类（字符串码，
    未被任何代码引用）。此处为 API 层统一异常的规范定义。
    """

    http_status = 422
    code = _codes.INVALID_PARAM


class ExternalServiceError(AppError):
    """外部服务失败：AI 模型、对象存储等 → HTTP 502；超时 → 504（timeout=True）。"""

    http_status = 502
    code = _codes.AI_PROVIDER_ERROR

    def __init__(self, message: str = "", *, timeout: bool = False, **kwargs: Any) -> None:
        if timeout and "code" not in kwargs:
            kwargs["code"] = _codes.AI_TIMEOUT
            kwargs["http_status"] = 504
        super().__init__(message, **kwargs)


class SystemError(AppError):
    """未预期的系统内部错误 → HTTP 500。message 不得泄露内部细节。"""

    http_status = 500
    code = _codes.SYSTEM_ERROR
