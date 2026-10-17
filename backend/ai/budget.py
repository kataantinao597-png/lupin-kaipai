"""124：模型调用预算控制。

- ai_budgets 表：org_id / monthly_limit_cents / used_cents / period
- 每次 AI 调用前检查：used >= limit → 抛 BudgetExceededError（API 层转 402）
- 调用成功后按 token 扣减 used_cents
- 管理员 API 可调整限额、重置用量

计费简化：1K tokens = 1 分（0.01 元）。正式计费以模型价格表为准，
这里只做"防止费用爆炸"的熔断，不做精确账单。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 1K tokens 折算多少分（1 分 = 0.01 元）
CENTS_PER_1K_TOKENS = 1


def _current_period() -> str:
    """当前预算周期：YYYY-MM。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_or_create_budget(db, org_id: str):
    """取本月预算行，不存在则创建（默认限额 0 = 未设置，需管理员配置）。"""
    from ..models import AIBudget
    period = _current_period()
    b = db.query(AIBudget).filter(
        AIBudget.org_id == org_id, AIBudget.period == period).first()
    if b is None:
        b = AIBudget(org_id=org_id, period=period,
                     monthly_limit_cents=0, used_cents=0)
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


def check_budget(db, org_id: str, task_type: str = ""):
    """124：调用前检查。超限抛 BudgetExceededError。

    限额为 0 表示管理员未配置 → 视为不限（兼容未启用预算的老组织），
    但记一条 warning 提醒配置。
    """
    from ..ai.gateway import BudgetExceededError
    b = get_or_create_budget(db, org_id)
    if b.monthly_limit_cents <= 0:
        logger.warning("组织 %s 本月 AI 预算未配置（限额为 0），本次不限制", org_id)
        return
    if b.used_cents >= b.monthly_limit_cents:
        raise BudgetExceededError(
            f"组织本月 AI 预算已用完（{b.used_cents}/{b.monthly_limit_cents} 分）。"
            f"任务 {task_type} 被拒绝。请联系管理员调整限额。")


def charge_usage(db, org_id: str, total_tokens: int) -> int:
    """调用成功后扣减。返回本次扣减的分。"""
    b = get_or_create_budget(db, org_id)
    cents = max(1, (total_tokens + 999) // 1000 * CENTS_PER_1K_TOKENS)
    b.used_cents = (b.used_cents or 0) + cents
    b.updated_at = datetime.now(timezone.utc)
    db.commit()
    if b.monthly_limit_cents > 0 and b.used_cents >= b.monthly_limit_cents:
        logger.warning("组织 %s 本月 AI 预算已达上限（%d/%d 分）",
                       org_id, b.used_cents, b.monthly_limit_cents)
    return cents


def set_limit(db, org_id: str, monthly_limit_cents: int):
    """管理员：设置/调整本月限额。"""
    b = get_or_create_budget(db, org_id)
    b.monthly_limit_cents = monthly_limit_cents
    b.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(b)
    return b


def reset_usage(db, org_id: str):
    """管理员：重置本月已用额度。"""
    b = get_or_create_budget(db, org_id)
    b.used_cents = 0
    b.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(b)
    return b
