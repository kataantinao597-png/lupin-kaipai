"""AI 调用台账的组织归属口径（唯一实现）。

背景（P1 修复，2026-10-20 实测）
--------------------------------
`ai_calls` 表原本没有 `org_id` 列，组织归属只能经
`ai_calls.task_id → ai_tasks.org_id` 推导。但同步 AI 接口
（/api/ideas、/api/storyboard、/api/feasibility、/api/risks、
/api/copy-optimize 等）不创建 ai_tasks 行，`task_id` 恒为 NULL，
于是三处统计口径全部漏计：

- `GET /api/v1/ai/usage/summary`（用量仪表盘）
- `GET /api/v1/ai/calls`（调用台账）
- `GET /api/v1/enterprise/{org}/usage/report`（组织用量报表）
- `quota_service.current_usage(ai_calls_daily)`（每日调用配额）

实测：库内 2181 条 ai_calls，其中 2178 条 task_id 为空，
仪表盘对任何组织都只显示个位数调用，配额也永远不会触发。

修复
----
`ai_calls.org_id` 直接记录调用时的已校验组织（网关 `_org_id`），
统计侧优先用它，并对**存量行**（org_id 为空的老数据）保留 task 关联回退，
这样历史数据不会凭空消失。本模块是这条口径的唯一实现，避免三处漂移。
"""
from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .. import models


def org_calls_query(db: Session, org_id: str):
    """返回本组织的 AI 调用查询（含存量行的 task 关联回退）。

    用法与原 `db.query(AICall).join(AITask, ...)` 一致，
    支持 `.with_entities(...)` / `.count()` / `.group_by(...)`。
    """
    return (
        db.query(models.AICall)
        .outerjoin(models.AITask, models.AICall.task_id == models.AITask.id)
        .filter(or_(
            models.AICall.org_id == org_id,
            and_(models.AICall.org_id.is_(None),
                 models.AITask.org_id == org_id),
        ))
    )
