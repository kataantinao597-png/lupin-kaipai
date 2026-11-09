"""AI 任务 Repository（035/036/038）。

- 全部写操作走 @transactional_method（032 事务边界）。
- 状态变更统一走 set_status()，内部调 validate_task_transition()（036）。
- 038 取消：request_cancel() 只打标记（cancel_requested=True），
  pending 任务由队列直接置 cancelled；running 任务由工作线程协作式检查。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import AITask, TASK_STATUSES, TASK_STATUS_CANCELLED, TASK_STATUS_PENDING
from ..repositories.base import (
    EntityNotFound,
    transactional,
    transactional_method,
)
from .state_machine import validate_task_transition


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AITaskRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, task_type: str,
               payload: dict[str, Any] | None = None,
               created_by: str = "",
               priority: str = "high") -> AITask:
        """创建 pending 任务。payload 已在 API 层脱敏（不含 Key）。

        137：priority=high（用户交互）/low（后台批量），队列优先执行 high。
        """
        if priority not in ("high", "low"):
            priority = "high"
        task = AITask(
            org_id=org_id, created_by=created_by, task_type=task_type,
            payload_json=payload or {}, priority=priority,
        )
        self.db.add(task)
        self.db.flush()
        self.db.refresh(task)
        return task

    def get(self, task_id: str, org_id: str | None = None) -> AITask | None:
        """取任务；org_id 非空时做租户隔离（048）。"""
        stmt = select(AITask).where(AITask.id == task_id)
        if org_id is not None:
            stmt = stmt.where(AITask.org_id == org_id)
        return self.db.scalar(stmt)

    def get_or_404(self, task_id: str, org_id: str | None = None) -> AITask:
        task = self.get(task_id, org_id)
        if task is None:
            raise EntityNotFound("AITask", task_id)
        return task

    def list_by_org(self, org_id: str, status: str | None = None,
                    limit: int = 50, offset: int = 0,
                    created_by: str | None = None) -> list[AITask]:
        """S-1：created_by 非空时只列该用户创建的任务（普通成员口径）；
        为 None 时列全组织（admin/owner 口径，调用方须先验 admin 身份）。"""
        stmt = select(AITask).where(AITask.org_id == org_id)
        if status is not None:
            if status not in TASK_STATUSES:
                raise ValueError(f"非法任务状态: {status}")
            stmt = stmt.where(AITask.status == status)
        if created_by is not None:
            stmt = stmt.where(AITask.created_by == created_by)
        return list(self.db.scalars(
            stmt.order_by(AITask.created_at.desc()).limit(limit).offset(offset)))

    @transactional_method
    def set_status(self, task_id: str, to_status: str,
                   result: dict[str, Any] | None = None,
                   error: str | None = None,
                   progress: int | None = None,
                   model_used: str | None = None) -> AITask:
        """036：状态流转唯一入口——先校验合法性，再原子更新。"""
        task = self.get(task_id)
        if task is None:
            raise EntityNotFound("AITask", task_id)
        validate_task_transition(task_id, task.status, to_status)
        values: dict[str, Any] = {
            "status": to_status, "updated_at": _utcnow(),
        }
        if result is not None:
            values["result_json"] = result
        if error is not None:
            values["error"] = error
        if progress is not None:
            values["progress"] = max(0, min(100, progress))
        if model_used is not None:
            values["model_used"] = model_used
        now = _utcnow()
        if to_status == "running":
            values["started_at"] = now
        if to_status in ("succeeded", "failed", "cancelled"):
            values["finished_at"] = now
        self.db.execute(
            update(AITask).where(AITask.id == task_id).values(**values))
        self.db.flush()
        task = self.get(task_id)
        assert task is not None
        return task

    @transactional_method
    def update_progress(self, task_id: str, progress: int) -> None:
        """更新 running 任务进度（不改变状态）。"""
        self.db.execute(
            update(AITask).where(AITask.id == task_id).values(
                progress=max(0, min(100, progress)), updated_at=_utcnow()))
        self.db.flush()

    @transactional_method
    def update_payload(self, task_id: str,
                       payload: dict[str, Any]) -> AITask:
        """Phase 3：整体替换 payload_json（run_status/pending_step 等扩展
        字段），不改变任务状态。"""
        task = self.get(task_id)
        if task is None:
            raise EntityNotFound("AITask", task_id)
        self.db.execute(
            update(AITask).where(AITask.id == task_id).values(
                payload_json=payload, updated_at=_utcnow()))
        self.db.flush()
        task = self.get(task_id)
        assert task is not None
        return task

    @transactional_method
    def request_cancel(self, task_id: str) -> AITask:
        """038：请求取消——只打标记，由队列/工作线程执行实际取消。

        返回更新后的任务。pending→cancelled 的直接取消由 TaskQueue 处理。
        """
        task = self.get(task_id)
        if task is None:
            raise EntityNotFound("AITask", task_id)
        # 终态任务：重复取消幂等，直接返回
        if task.status in ("succeeded", "failed", "cancelled"):
            return task
        task.cancel_requested = True
        task.updated_at = _utcnow()
        self.db.flush()
        self.db.refresh(task)
        return task

    def is_cancel_requested(self, task_id: str) -> bool:
        """工作线程协作式检查：是否被请求取消。独立短会话查询。"""
        val = self.db.scalar(
            select(AITask.cancel_requested).where(AITask.id == task_id))
        return bool(val)
