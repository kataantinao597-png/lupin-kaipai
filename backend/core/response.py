"""027：统一 API 响应约定（仅 /api/v1/* 使用）。

成功：
    {"code": 0, "data": {...}, "request_id": "..."}
失败：
    {"code": 非0, "message": "...", "request_id": "...", "details": {...}}

老 /api/* 接口保持原有响应格式不变（见 app.py 顶部 TODO）。
204 No Content（删除类接口）无响应体，视为成功，不套信封。
"""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

from .request_id import get_request_id

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """成功响应信封。data 为业务数据，request_id 用于全链路追踪。"""

    code: int = Field(default=0, description="业务码，0=成功")
    data: Optional[T] = Field(default=None, description="业务数据")
    request_id: str = Field(default="", description="请求 ID（响应头 X-Request-ID 同值）")


class ApiError(BaseModel):
    """失败响应信封。"""

    code: int = Field(description="业务错误码，见 core/error_codes.py")
    message: str = Field(description="面向用户的错误说明")
    request_id: str = Field(default="")
    details: Any = Field(default=None, description="调试细节（生产环境不含敏感信息）")


def success(data: Any = None, request_id: Optional[str] = None) -> dict:
    """构造成功响应。配合 response_model=ApiResponse[YourModel] 使用。"""
    return {
        "code": 0,
        "data": data,
        "request_id": request_id or get_request_id(),
    }


def error(
    code: int,
    message: str,
    request_id: Optional[str] = None,
    details: Any = None,
) -> dict:
    """构造失败响应。"""
    return {
        "code": code,
        "message": message,
        "request_id": request_id or get_request_id(),
        "details": details,
    }


def paginated(
    items: list,
    total: int,
    page: int,
    page_size: int,
    request_id: Optional[str] = None,
) -> dict:
    """分页响应（预留，当前 v1 接口暂未使用分页）。"""
    return success(
        {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        request_id=request_id,
    )
