"""Phase 5（133/134）：AI 可观测性查询接口。

- GET /api/v1/ai/calls          调用台账：分页 + task_type/model/时间范围过滤
- GET /api/v1/ai/usage/summary  用量仪表盘：今日/本月 token 与费用，各模型/任务类型分布

认证：与现有 MVP 接口保持一致——不强制登录（Phase 8 再收紧）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ...auth.database import get_db
from ...models import AICall

router = APIRouter(prefix="/ai", tags=["v1-ai-observability"])


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


@router.get("/calls")
def list_calls(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    task_type: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None, description="ISO 时间起点"),
    end: Optional[str] = Query(default=None, description="ISO 时间终点"),
    db: Session = Depends(get_db),
):
    """133：分页查询 AI 调用记录。"""
    q = db.query(AICall)
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
    db: Session = Depends(get_db),
):
    """134：用量仪表盘。返回今日/本月 token 与费用、各模型/任务类型分布。"""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    range_start = now - timedelta(days=days)

    def _totals(since: datetime):
        row = (db.query(
            func.coalesce(func.sum(AICall.total_tokens), 0),
            func.coalesce(func.sum(AICall.cost_cents), 0),
            func.count(AICall.id),
        ).filter(AICall.created_at >= since).one())
        return {"tokens": int(row[0]), "cost_cents": int(row[1]),
                "calls": int(row[2])}

    def _by(field, since: datetime):
        rows = (db.query(
            field,
            func.coalesce(func.sum(AICall.total_tokens), 0),
            func.coalesce(func.sum(AICall.cost_cents), 0),
            func.count(AICall.id),
        ).filter(AICall.created_at >= since)
            .group_by(field).order_by(desc(func.sum(AICall.total_tokens)))
            .all())
        return [{"name": r[0], "tokens": int(r[1]),
                 "cost_cents": int(r[2]), "calls": int(r[3])}
                for r in rows]

    return {
        "today": _totals(day_start),
        "month": _totals(month_start),
        "range_days": days,
        "range": _totals(range_start),
        "by_model": _by(AICall.model, range_start),
        "by_task_type": _by(AICall.task_type, range_start),
        "currency": "CNY",
    }
