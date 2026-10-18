"""128/129：任务选模型策略 + 模型能力注册表管理 API。

- GET  /api/v1/ai/model-policy              查看全任务模型策略
- PUT  /api/v1/ai/model-policy/{task_type}  管理员调整某任务模型/兜底
- GET  /api/v1/ai/model-capabilities        查看模型能力注册表
- PUT  /api/v1/ai/model-capabilities/{model} 管理员更新模型能力

写操作要求：登录用户且至少在一个组织内担任 owner/admin
（全局 AI 策略影响所有租户，普通成员不可改）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai.capabilities import (
    list_capabilities,
    set_capability,
)
from ...ai.model_policy import list_policies, set_policy
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core.response import success
from ...rbac import ROLE_ADMIN, ROLE_OWNER, get_membership

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["v1-ai-models"])


def _require_platform_admin(user: User, db: Session) -> None:
    """任一组织的 owner/admin 即可管理全局 AI 策略；否则 403。"""
    from ... import models
    roles = [
        m.role for m in
        db.query(models.Membership).filter(
            models.Membership.user_id == user.id).all()
    ]
    if not any(r in (ROLE_OWNER, ROLE_ADMIN) for r in roles):
        raise HTTPException(status_code=403,
                            detail="需要组织管理员（owner/admin）权限")


class ModelPolicyIn(BaseModel):
    model: str = Field(..., description="主模型名")
    fallback_model: str | None = Field(
        default=None, description="兜底模型名（130 故障切换用），空为不设")


class ModelCapabilityIn(BaseModel):
    max_tokens: int = Field(default=0, description="上下文长度，0=未知")
    supports_vision: bool = False
    supports_json: bool = True
    cost_tier: str = Field(default="standard",
                           description="成本档：cheap/standard/premium")


@router.get("/model-policy", response_model=dict)
def get_model_policy(db: Session = Depends(get_db)):
    """128：查看全任务模型策略（登录即可）。"""
    return success({"policies": list_policies(db)})


@router.put("/model-policy/{task_type}", response_model=dict)
def put_model_policy(task_type: str, req: ModelPolicyIn,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """128：管理员调整某任务的模型/兜底模型。"""
    _require_platform_admin(user, db)
    try:
        return success(set_policy(db, task_type, req.model,
                                  req.fallback_model))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/model-capabilities", response_model=dict)
def get_model_capabilities(db: Session = Depends(get_db)):
    """129：查看模型能力注册表（登录即可）。"""
    return success({"capabilities": list_capabilities(db)})


@router.put("/model-capabilities/{model}", response_model=dict)
def put_model_capability(model: str, req: ModelCapabilityIn,
                         user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """129：管理员更新模型能力。"""
    _require_platform_admin(user, db)
    try:
        return success(set_capability(db, model, req.max_tokens,
                                      req.supports_vision, req.supports_json,
                                      req.cost_tier))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
