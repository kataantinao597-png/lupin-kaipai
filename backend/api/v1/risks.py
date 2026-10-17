"""022：风险审核与交付检查路由（从 app.py 拆分）。

- POST /api/risk-check      AI 风险审核 + 事实 grounding 规则层
- POST /api/delivery-check  交付状态检查（plan/final 两套标准）

业务逻辑在 services/risks_service.py 与 services/delivery_service.py。
"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...core.deps import check_rate_limit, get_ai_gateway, get_request_model
from ...services.delivery_service import check_delivery
from ...services.risk_service import check_storyboard_risk
from .ideas import ProductInput

router = APIRouter(tags=["risks"])


class RiskCheckRequest(BaseModel):
    storyboard: dict
    evidence_materials: str = ""
    selling_points: List[str] = Field(default_factory=list)
    product: Optional[ProductInput] = None
    facts: List[dict] = Field(default_factory=list)


class DeliveryCheckRequest(BaseModel):
    storyboard: Optional[dict] = None
    product: Optional[dict] = None
    facts: List[dict] = Field(default_factory=list)
    feasibility: Optional[dict] = None
    shoot_state: dict = Field(default_factory=dict)
    # 002: 两套交付标准 —— "plan" 策划方案可交付 / "final" 最终成片可交付
    check_type: Literal["plan", "final"] = "plan"


@router.post("/api/risk-check")
def check_risk(
    req: RiskCheckRequest,
    request: Request,
    gw=Depends(get_ai_gateway),
):
    check_rate_limit(request)
    _model = get_request_model(request)
    try:
        from ...prompts import product_block
        pinfo = product_block(req.product.model_dump()) if req.product else ""
        result = check_storyboard_risk(
            gw, req.storyboard,
            evidence_materials=req.evidence_materials,
            selling_points=req.selling_points,
            product_info=pinfo,
            facts=req.facts,
            model=_model)
    except BudgetExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except TaskLimitExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"风险复核失败: {e}")
    return result


@router.post("/api/delivery-check")
def delivery_check_route(req: DeliveryCheckRequest):
    # 021：业务逻辑已迁 services/delivery_service.py
    return check_delivery(
        storyboard=req.storyboard,
        product=req.product,
        facts=req.facts,
        feasibility=req.feasibility,
        shoot_state=req.shoot_state,
        check_type=req.check_type,
    )
