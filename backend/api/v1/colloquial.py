"""农人口语改写器（同步接口）。

- POST /api/colloquial-rewrite  输入一段书面口播文案 → AI 改写成山东农人口语味

无 AI 配置时 503 并明示，不伪造改写内容（区别于 feasibility 的规则降级：
改写没有可用的确定性规则，只能诚实失败）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core.deps import get_ai_gateway, get_request_model
from ...core.idempotency import idempotency_begin
from ...rbac import get_membership
from ...ai.prompts import get_prompt_version
from ...ai.prompts.colloquial_v1 import build_colloquial_messages

router = APIRouter(tags=["colloquial"])


class ColloquialRewriteRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000,
                      description="待改写的书面口播文案")
    product_name: str = Field(default="", max_length=100,
                              description="产品名（可选，帮助改写贴合）")


def _validate_colloquial(data: dict):
    """校验改写结果结构。返回 (errors, data)。"""
    errors = []
    if not isinstance(data, dict):
        return ["[结构] 返回不是 JSON 对象"], None
    rw = data.get("rewritten")
    if not isinstance(rw, str) or not rw.strip():
        errors.append("[结构] 缺少 rewritten 字段")
    changes = data.get("changes", [])
    if not isinstance(changes, list):
        errors.append("[结构] changes 不是数组")
    if errors:
        return errors, None
    return [], data


@router.post("/api/colloquial-rewrite")
def rewrite_colloquial(
    req: ColloquialRewriteRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gw=Depends(get_ai_gateway),
):
    """农人口语改写：书面口播 → 山东农人口语味。

    V3.1 P0-2：强制登录；网关经 X-Org-Id 头取组织做预算——若携带
    该头则校验调用者为该组织成员（不存在/无权一律 404，防组织枚举），
    避免用别人的组织配额刷接口。
    """
    header_org = (request.headers.get("X-Org-Id") or "").strip() or None
    if header_org and get_membership(db, user.id, header_org) is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    _model = get_request_model(request)
    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("colloquial-rewrite", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay

    def make_messages(feedback):
        return build_colloquial_messages(req.text.strip(),
                                         req.product_name.strip())

    try:
        try:
            result, meta = gw.generate_with_retry(
                "colloquial_rewrite", make_messages, _validate_colloquial,
                model=_model or None, temperature=0.8,
                prompt_version=get_prompt_version("colloquial_rewrite"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            # 无 AI 可用时诚实失败：改写无确定性规则可降级，不伪造内容
            raise HTTPException(
                status_code=503,
                detail=f"AI 改写服务暂不可用（{e}），请稍后再试")
        resp = {"rewritten": result["rewritten"],
                "changes": result.get("changes") or [],
                "attempts": meta["attempts"]}
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)
