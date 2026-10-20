"""Phase 5（133/134）：AI 可观测性查询接口。

- GET /api/v1/ai/calls          调用台账：分页 + task_type/model/时间范围过滤
- GET /api/v1/ai/usage/summary  用量仪表盘：今日/本月 token 与费用，各模型/任务类型分布

权限（2026-10-19 收紧）：
- 必须登录。
- 必须显式传 `org_id`，且当前用户是该组织的 owner/admin。
- 非成员或角色不足 → 403；组织不存在 → 404。

口径（2026-10-20 P1 修复）：
- 归属优先取 `ai_calls.org_id`（同步/异步都写），存量行回退
  `task_id → ai_tasks.org_id`。统一实现在 `services/ai_ledger.py`。
  修复前同步调用 task_id 为空、无法归属，实测 2178/2181 条调用被漏计，
  仪表盘对任何组织都显示个位数。

收紧原因：早期这两个接口无需登录且返回全平台口径，任何访客都能读到
所有组织的 token 用量与费用明细。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...models import AICall
from ...rbac import ROLE_ADMIN, ROLE_RANK, get_membership

router = APIRouter(prefix="/ai", tags=["v1-ai-observability"])

NO_PERMISSION = "权限不足，无法查看 AI 用量"


def _org_admin(
    org_id: str = Query(..., description="组织 ID；只能查自己所属组织"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> str:
    """校验：登录 + 是该组织 owner/admin。通过后返回 org_id。"""
    org = (db.query(models.Organization)
             .filter(models.Organization.id == org_id).first())
    if org is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    ms = get_membership(db, user.id, org_id)
    if ms is None or ROLE_RANK.get(ms.role, 0) < ROLE_RANK[ROLE_ADMIN]:
        raise AuthError(NO_PERMISSION, forbidden=True)
    return org_id


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _org_calls(db: Session, org_id: str):
    """本组织的调用台账查询（org_id 直取 + 存量行 task 回退）。"""
    from ...services.ai_ledger import org_calls_query
    return org_calls_query(db, org_id)


@router.get("/calls")
def list_calls(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    task_type: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None, description="ISO 时间起点"),
    end: Optional[str] = Query(default=None, description="ISO 时间终点"),
    org_id: str = Depends(_org_admin),
    db: Session = Depends(get_db),
):
    """133：分页查询 AI 调用记录（仅本组织，仅 owner/admin）。"""
    q = _org_calls(db, org_id)
    if task_type:
        q = q.filter(AICall.task_type == task_type)
    if model:
        q = q.filter(AICall.model == model)
    s_dt, e_dt = _parse_dt(start), _parse_dt(end)
    if s_dt:
        q = q.filter(AICall.created_at >= s_dt)
    if e_dt:
        q = q.filter(AICall.created_at <= e_dt)
    total = q.count()
    rows = (q.order_by(desc(AICall.created_at))
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "org_id": org_id,
        "items": [{
            "id": r.id, "task_id": r.task_id, "task_type": r.task_type,
            "model": r.model, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens, "cost_cents": r.cost_cents,
            "status": r.status, "error": r.error,
            "latency_ms": r.latency_ms, "cached": bool(r.cached),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


@router.get("/usage/summary")
def usage_summary(
    days: int = Query(default=30, ge=1, le=365,
                      description="统计最近 N 天（默认 30）"),
    org_id: str = Depends(_org_admin),
    db: Session = Depends(get_db),
):
    """134：用量仪表盘（仅本组织，仅 owner/admin）。

    口径说明：`ai_calls.org_id` 归属（同步/异步都写），存量行回退
    `task_id → ai_tasks.org_id`；匿名调用（无组织）不计入任何组织。
    """
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    range_start = now - timedelta(days=days)

    def _totals(since: datetime):
        row = (_org_calls(db, org_id)
               .with_entities(
                   func.coalesce(func.sum(AICall.total_tokens), 0),
                   func.coalesce(func.sum(AICall.cost_cents), 0),
                   func.count(AICall.id))
               .filter(AICall.created_at >= since).one())
        return {"tokens": int(row[0]), "cost_cents": int(row[1]),
                "calls": int(row[2])}

    def _by(field, since: datetime):
        rows = (_org_calls(db, org_id)
                .with_entities(
                    field,
                    func.coalesce(func.sum(AICall.total_tokens), 0),
                    func.coalesce(func.sum(AICall.cost_cents), 0),
                    func.count(AICall.id))
                .filter(AICall.created_at >= since)
                .group_by(field)
                .order_by(desc(func.sum(AICall.total_tokens)))
                .all())
        return [{"name": r[0], "tokens": int(r[1]),
                 "cost_cents": int(r[2]), "calls": int(r[3])}
                for r in rows]

    return {
        "org_id": org_id,
        "today": _totals(day_start),
        "month": _totals(month_start),
        "range_days": days,
        "range": _totals(range_start),
        "by_model": _by(AICall.model, range_start),
        "by_task_type": _by(AICall.task_type, range_start),
        "currency": "CNY",
        "note": "按 ai_calls.org_id 归属（同步调用同样计入）；匿名调用不属于任何组织",
    }
