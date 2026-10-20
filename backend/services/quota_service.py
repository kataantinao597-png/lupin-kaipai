"""S10 配额系统：组织维度硬限额。

quota_type:
- ai_calls_daily：组织每日 AI 调用次数（含同步/异步）。归属口径见
  `services/ai_ledger.org_calls_query`：优先 ai_calls.org_id，存量行回退
  ai_tasks 关联。修复前同步调用（task_id 为空）不计入，配额永不触发。
- storage_mb：作品 data_json 总量（MB，向下取整；不含回收站）
- members：组织成员数

limit_value <= 0 或无记录 → 视为未配置（不限）。
超限抛 QuotaExceededError（BudgetExceededError 子类，API 层已映射为 402）。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func

from ..ai.gateway import BudgetExceededError
from .. import models

logger = logging.getLogger(__name__)

QUOTA_AI_CALLS_DAILY = "ai_calls_daily"
QUOTA_STORAGE_MB = "storage_mb"
QUOTA_MEMBERS = "members"
QUOTA_TYPES = frozenset({QUOTA_AI_CALLS_DAILY, QUOTA_STORAGE_MB, QUOTA_MEMBERS})


class QuotaExceededError(BudgetExceededError):
    """S10：配额超限。继承 BudgetExceededError，复用 API 层 402 映射。"""


def get_quota(db, org_id: str, quota_type: str):
    """取配额记录；无记录返回 None（不限）。"""
    return (
        db.query(models.Quota)
        .filter(models.Quota.org_id == org_id,
                models.Quota.quota_type == quota_type)
        .first()
    )


def set_quota(db, org_id: str, quota_type: str, limit_value: int):
    """设置配额。limit_value <= 0 表示取消限制（删除记录）。"""
    if quota_type not in QUOTA_TYPES:
        raise ValueError(f"未知配额类型: {quota_type}")
    q = get_quota(db, org_id, quota_type)
    if limit_value is None or int(limit_value) <= 0:
        if q is not None:
            db.delete(q)
            db.commit()
        return None
    if q is None:
        q = models.Quota(org_id=org_id, quota_type=quota_type,
                         limit_value=int(limit_value))
        db.add(q)
    else:
        q.limit_value = int(limit_value)
    db.commit()
    db.refresh(q)
    return q


def _day_start_naive() -> datetime:
    """今天 0 点（naive UTC；SQLite 存的 DateTime(timezone=True) 读出为 naive）。"""
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)


def current_usage(db, org_id: str, quota_type: str) -> int:
    """当前用量。"""
    if quota_type == QUOTA_AI_CALLS_DAILY:
        day_start = _day_start_naive()
        from .ai_ledger import org_calls_query
        return (
            org_calls_query(db, org_id)
            .filter(models.AICall.created_at >= day_start)
            .count() or 0
        )
    if quota_type == QUOTA_STORAGE_MB:
        total_bytes = (
            db.query(func.coalesce(func.sum(func.length(models.Work.data_json)), 0))
            .filter(models.Work.org_id == org_id,
                    models.Work.deleted_at.is_(None))
            .scalar() or 0
        )
        return int(total_bytes) // (1024 * 1024)
    if quota_type == QUOTA_MEMBERS:
        return (
            db.query(func.count(models.Membership.id))
            .filter(models.Membership.org_id == org_id)
            .scalar() or 0
        )
    raise ValueError(f"未知配额类型: {quota_type}")


def check_quota(db, org_id: str, quota_type: str) -> None:
    """检查配额；超限抛 QuotaExceededError。未配置视为不限。"""
    q = get_quota(db, org_id, quota_type)
    if q is None or (q.limit_value or 0) <= 0:
        return
    usage = current_usage(db, org_id, quota_type)
    if usage >= q.limit_value:
        raise QuotaExceededError(
            f"组织配额已用完（{quota_type}: {usage}/{q.limit_value}）。"
            "请联系管理员调整配额后重试。")
