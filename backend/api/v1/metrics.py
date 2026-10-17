"""S9.3：生产指标端点。

GET /api/v1/metrics —— 聚合返回：
- api: 请求量/成功率/P95 延迟（内存聚合，按路由模板）
- ai_tasks: 各状态计数、成功率（查 ai_tasks 表）
- ai_calls: 超时数、重试数（内存聚合）、token 总量、估算费用（查 ai_calls 表）
- queue: 队列积压（pending/queued/running 的 ai_tasks 数）
- db: 数据库错误率（内存聚合）

认证：与现有 MVP 接口一致，不强制登录（Phase 8 安全收紧时加）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth.database import get_db
from ...core import metrics as _m
from ...models import AICall, AITask

router = APIRouter(tags=["v1-metrics"])


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)):
    """S9.3：生产指标聚合。"""
    mem = _m.snapshot()

    # AI 任务：各状态计数 + 成功率
    task_rows = (
        db.query(AITask.status, func.count(AITask.id))
        .group_by(AITask.status)
        .all()
    )
    task_by_status = {s: c for s, c in task_rows}
    task_total = sum(task_by_status.values())
    task_done = task_by_status.get("succeeded", 0)
    task_failed = task_by_status.get("failed", 0)
    task_finished = task_done + task_failed

    # 队列积压：pending/queued/running
    backlog = sum(
        task_by_status.get(s, 0)
        for s in ("pending", "queued", "running", "retry_wait")
    )

    # AI 调用：token 总量、费用、状态分布
    call_agg = (
        db.query(
            func.coalesce(func.sum(AICall.total_tokens), 0),
            func.coalesce(func.sum(AICall.cost_cents), 0),
            func.count(AICall.id),
        )
        .one()
    )
    call_by_status = dict(
        db.query(AICall.status, func.count(AICall.id))
        .group_by(AICall.status)
        .all()
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api": mem["api"],
        "db": mem["db"],
        "ai_tasks": {
            "total": task_total,
            "by_status": task_by_status,
            "success_rate": round(task_done / task_finished, 4)
            if task_finished else 1.0,
            "backlog": backlog,
        },
        "ai_calls": {
            "total": int(call_agg[2] or 0),
            "by_status": call_by_status,
            "total_tokens": int(call_agg[0] or 0),
            "total_cost_cents": int(call_agg[1] or 0),
            "timeouts": mem["ai"]["timeouts"],
            "retries": mem["ai"]["retries"],
        },
    }
