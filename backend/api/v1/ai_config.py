"""123/124：AI 配置预检 + 预算管理 API。

- POST /api/v1/ai/config/validate   模型配置预检（Base URL 可达 / Key 有效 / 模型名存在）
- GET  /api/v1/ai/budgets/{org_id}  查看组织本月预算
- PUT  /api/v1/ai/budgets/{org_id}  管理员调整限额
- POST /api/v1/ai/budgets/{org_id}/reset  管理员重置已用额度
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai_service import AIConfigError, AIService
from ...auth.database import get_db
from ...core.response import success

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["v1-ai-config"])


class ConfigValidateRequest(BaseModel):
    base_url: str = Field(..., description="API Base，如 https://deepkey.top/v1")
    api_key: str = Field(..., description="API Key（仅用于本次预检，不存储不记日志）")
    model: str = Field(..., description="模型名，如 gpt-6-luna")


class ConfigValidateResult(BaseModel):
    ok: bool
    base_reachable: bool
    key_valid: bool
    model_available: bool
    detail: str = ""
    provider: str = ""
    config_version: str = ""


@router.post("/config/validate", response_model=dict)
def validate_config(req: ConfigValidateRequest):
    """123：模型配置预检。保存配置前必须先通过。

    检查三项：
    1. base_url 可达（URL 格式合法 + 能建连）
    2. api_key 有效（发一个最小 chat 请求；401 → Key 无效）
    3. 模型名存在（请求返回的 model 字段与请求一致，或至少不报 404/model_not_found）
    Key 只用于本次预检，不写入任何存储，不记入日志。
    """
    from ...ai.config import build_config, detect_provider
    from ...security.ssrf import SSRFError, validate_outbound_url

    base_url = req.base_url.strip().rstrip("/")
    model = req.model.strip()
    if not base_url.startswith(("http://", "https://")):
        return success(ConfigValidateResult(
            ok=False, base_reachable=False, key_valid=False,
            model_available=False,
            detail="Base URL 必须以 http:// 或 https:// 开头").model_dump())

    # Phase 9-2：SSRF 纵深校验（内网 IP / 保留地址 / DNS 重绑定）
    try:
        validate_outbound_url(base_url, purpose="模型 Base URL")
    except SSRFError as e:
        return success(ConfigValidateResult(
            ok=False, base_reachable=False, key_valid=False,
            model_available=False, detail=f"SSRF 拦截：{e}").model_dump())

    # 1. 构造 AIService（URL 格式校验 + deepkey 兜底都在 __init__ 里）
    try:
        svc = AIService(api_key=req.api_key, base_url=base_url)
    except AIConfigError as e:
        return success(ConfigValidateResult(
            ok=False, base_reachable=False, key_valid=False,
            model_available=False, detail=f"URL 无效: {e}").model_dump())

    cfg = build_config(api_key=req.api_key, base_url=svc.endpoint,
                       requested_model=model)

    # 2+3. 发最小 chat 请求验证 Key 与模型
    # 用 max_tokens=1 的极小请求，成本可忽略
    try:
        data = svc._post({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        })
    except Exception as e:
        from ...ai_service import AIFatalError, AIRetryableError
        msg = str(e)
        if isinstance(e, AIFatalError) and "401" in msg:
            return success(ConfigValidateResult(
                ok=False, base_reachable=True, key_valid=False,
                model_available=False, provider=cfg.provider,
                config_version=cfg.config_version,
                detail="API Key 无效（401）：请检查 Key 是否正确或已过期").model_dump())
        if isinstance(e, AIFatalError) and ("404" in msg or "model" in msg.lower()):
            return success(ConfigValidateResult(
                ok=False, base_reachable=True, key_valid=True,
                model_available=False, provider=cfg.provider,
                config_version=cfg.config_version,
                detail=f"模型名不存在：{msg[:120]}").model_dump())
        if isinstance(e, AIRetryableError):
            return success(ConfigValidateResult(
                ok=False, base_reachable=False, key_valid=False,
                model_available=False, provider=cfg.provider,
                config_version=cfg.config_version,
                detail=f"Base URL 不可达：{msg[:120]}").model_dump())
        return success(ConfigValidateResult(
            ok=False, base_reachable=False, key_valid=False,
            model_available=False, provider=cfg.provider,
            config_version=cfg.config_version,
            detail=f"预检失败：{msg[:120]}").model_dump())

    return success(ConfigValidateResult(
        ok=True, base_reachable=True, key_valid=True, model_available=True,
        provider=cfg.provider, config_version=cfg.config_version,
        detail="预检通过：Base 可达、Key 有效、模型可用").model_dump())


# ---------- 124：预算管理 ----------

class BudgetOut(BaseModel):
    org_id: str
    period: str
    monthly_limit_cents: int
    used_cents: int
    remaining_cents: int


def _budget_out(b) -> dict:
    return BudgetOut(
        org_id=b.org_id, period=b.period,
        monthly_limit_cents=b.monthly_limit_cents or 0,
        used_cents=b.used_cents or 0,
        remaining_cents=max(0, (b.monthly_limit_cents or 0) - (b.used_cents or 0)),
    ).model_dump()


@router.get("/budgets/{org_id}", response_model=dict)
def get_budget(org_id: str, db: Session = Depends(get_db)):
    """查看组织本月 AI 预算。"""
    from ...ai.budget import get_or_create_budget
    b = get_or_create_budget(db, org_id)
    return success(_budget_out(b))


class BudgetLimitUpdate(BaseModel):
    monthly_limit_cents: int = Field(..., ge=0,
                                     description="月限额（分）；0 = 不限")


@router.put("/budgets/{org_id}", response_model=dict)
def set_budget_limit(org_id: str, req: BudgetLimitUpdate,
                     db: Session = Depends(get_db)):
    """124：管理员调整组织本月 AI 预算限额。"""
    # TODO(Phase 4 权限组)：此处应校验调用者为 org admin/owner。
    # 当前按"默认全开"施工，先不做鉴权，部署前必须补上。
    from ...ai.budget import set_limit
    b = set_limit(db, org_id, req.monthly_limit_cents)
    logger.info("管理员调整组织 %s AI 预算限额为 %d 分", org_id, req.monthly_limit_cents)
    return success(_budget_out(b))


@router.post("/budgets/{org_id}/reset", response_model=dict)
def reset_budget(org_id: str, db: Session = Depends(get_db)):
    """124：管理员重置组织本月已用额度。"""
    from ...ai.budget import reset_usage
    b = reset_usage(db, org_id)
    logger.info("管理员重置组织 %s 本月 AI 预算用量", org_id)
    return success(_budget_out(b))
