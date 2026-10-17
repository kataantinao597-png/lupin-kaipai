"""API v1 的 Pydantic 请求/响应模型。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---- 组织 ----
class OrgCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class OrgOut(BaseModel):
    id: str
    name: str
    created_by: str
    created_at: datetime
    my_role: str


class InviteCreate(BaseModel):
    role: str = Field(default="viewer", description="被邀请者加入后的默认角色")


class InviteOut(BaseModel):
    code: str
    org_id: str
    role: str
    expires_at: datetime


class JoinRequest(BaseModel):
    code: str = Field(..., min_length=1)


class MemberOut(BaseModel):
    user_id: str
    role: str
    joined_at: datetime


class MemberRoleUpdate(BaseModel):
    role: str = Field(...)


# ---- 作品 ----
class WorkCreate(BaseModel):
    org_id: str = Field(..., description="作品所属组织，创建时必须指定")
    title: str = Field(default="", max_length=256)
    data: dict[str, Any] = Field(default_factory=dict)


class WorkUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    data: Optional[dict[str, Any]] = None
    status: Optional[str] = Field(default=None, description="draft|active|archived")
    # 088：提供时做乐观锁检查，不匹配返回 409；不提供则保持兼容直接更新
    version: Optional[int] = Field(default=None, description="乐观锁版本号")


class WorkOut(BaseModel):
    id: str
    org_id: str
    owner_id: str
    title: str
    data: dict[str, Any]
    status: str = "draft"
    version: int = 1
    created_at: datetime
    updated_at: datetime


# ---- 作品历史版本（091） ----
class WorkVersionOut(BaseModel):
    id: str
    work_id: str
    version_no: int
    title: str
    created_by: str
    created_at: datetime


# ---- 回收站（092）：复用 WorkOut，加 deleted_at ----
class TrashWorkOut(WorkOut):
    deleted_at: Optional[datetime] = None
