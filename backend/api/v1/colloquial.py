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
from ...ai.secure_messages import _secure_messages
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core.deps import get_ai_gateway, get_request_model
from ...core.idempotency import idempotency_begin
from ...rbac import get_membership
from ...repositories.fact_repository import FactRepository
from ...ai.prompts import get_prompt_version
from ...ai.prompts.colloquial_v2 import build_colloquial_messages

router = APIRouter(tags=["colloquial"])


class ColloquialRewriteRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000,
                      description="待改写的书面口播文案")
    product_name: str = Field(default="", max_length=100,
                              description="产品名（可选，帮助改写贴合）")
    # V3.1 §4.1：可选传入作品/产品，服务端据此核对已确认事实注入 prompt
    work_id: str | None = Field(default=None, description="作品 id（可选）")
    product_id: str | None = Field(default=None, description="产品档案 id（可选）")


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

    V3.1 §4.1：事实保真——服务端按组织（+产品）从事实库取出
    confirmed + allow_use + 未过期的事实注入 prompt；绝不信任前端
    传入的确认标志。无组织头时 facts 为空，改写仅基于原文。
    """
    header_org = ((request.headers.get("X-Org-Id") or "").strip()
                  or (request.query_params.get("org_id") or "").strip()
                  or None)
    if header_org and get_membership(db, user.id, header_org) is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    _model = get_request_model(request)
    # V3.1 §4.1：服务端核对的已确认事实（只取可用事实）
    facts = []
    if header_org:
        try:
            usable = FactRepository(db).list_usable(
                header_org, req.product_id, limit=50)
            facts = [{"id": f.id, "content": f.content,
                      "category": f.category or "其他",
                      "source": f.source_ref or "",
                      "status": "confirmed", "allow_use": True}
                     for f in usable]
        except Exception:  # noqa: BLE001
            # 事实库读取失败不阻断改写：降级为无事实模式（日志留痕）
            import logging
            logging.getLogger(__name__).warning(
                "拉呱版事实读取失败，降级为无事实模式", exc_info=True)
            facts = []
    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("colloquial-rewrite", request, req.model_dump(),
                             scope=_model or "", user_id=user.id)
    if idem.replay is not None:
        return idem.replay

    prompt_version = get_prompt_version("colloquial_rewrite")

    def make_messages(feedback):
        # S-3 同类闭合：与 runner 同款隔离（已登录接口的自注入面）
        return _secure_messages(build_colloquial_messages(
            req.text.strip(), req.product_name.strip(), facts))

    try:
        try:
            result, meta = gw.generate_with_retry(
                "colloquial_rewrite", make_messages, _validate_colloquial,
                model=_model or None, temperature=0.8,
                prompt_version=prompt_version)
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
                "attempts": meta["attempts"],
                "prompt_version": prompt_version,
                "facts_used": len(facts)}
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)
