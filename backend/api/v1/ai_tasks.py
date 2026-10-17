"""AI 异步任务 API（034/037/038）。

- POST   /api/v1/ai-tasks                  创建任务 → {task_id, status}
- GET    /api/v1/ai-tasks/{task_id}        查询状态/结果（037）
- POST   /api/v1/ai-tasks/{task_id}/cancel 取消任务（038）
- GET    /api/v1/ai-tasks                  按组织列出任务（可选 ?status=）

认证：与现有 /api/ideas 等 MVP 接口保持一致——不强制登录。
org_id 可选：提供则做租户隔离；不提供则使用内置 default 组织
（自动建），保证未登录的 MVP 流程可用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...ai_tasks.queue import TaskQueue
from ...ai_tasks.repository import AITaskRepository
from ...ai_tasks.runners import RUNNERS
from ...ai_tasks.state_machine import is_terminal
from ...auth.database import get_db, get_session_factory
from ...models import TASK_TYPES, Organization
from ...repositories.base import EntityNotFound
from .schemas import (
    AITaskCancelOut,
    AITaskCreate,
    AITaskListOut,
    AITaskOut,
)

router = APIRouter(prefix="/ai-tasks", tags=["v1-ai-tasks"])

DEFAULT_ORG_NAME = "default"


def _ensure_default_org(db: Session) -> Organization:
    org = db.scalar(
        select(Organization).where(Organization.name == DEFAULT_ORG_NAME))
    if org is None:
        org = Organization(name=DEFAULT_ORG_NAME, created_by="system")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org


def _resolve_org_id(db: Session, org_id: Optional[str]) -> str:
    if org_id:
        org = db.scalar(
            select(Organization).where(Organization.id == org_id))
        if org is None:
            raise HTTPException(status_code=404, detail="组织不存在")
        return org.id
    return _ensure_default_org(db).id


def _get_queue() -> TaskQueue:
    return TaskQueue(get_session_factory())


def _task_out(task) -> AITaskOut:
    return AITaskOut(
        id=task.id, org_id=task.org_id, task_type=task.task_type,
        status=task.status, progress=task.progress or 0,
        result=task.result_json, error=task.error,
        model_used=task.model_used, priority=task.priority or "high",
        created_at=task.created_at, started_at=task.started_at,
        finished_at=task.finished_at,
    )


@router.post("", response_model=AITaskOut, status_code=202)
def create_task(req: AITaskCreate, db: Session = Depends(get_db)):
    """034：创建 AI 异步任务，返回 task_id（202 Accepted）。

    payload 与同步接口请求体结构一致。注意：异步任务只用服务端
    配置的 AI（环境变量），请求头自带 Key 的请走同步接口。
    """
    if req.task_type not in TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的任务类型: {req.task_type}，可选 {list(TASK_TYPES)}")
    if req.task_type not in RUNNERS:
        raise HTTPException(
            status_code=400,
            detail=f"任务类型 {req.task_type} 暂不支持异步执行")
    org_id = _resolve_org_id(db, req.org_id)
    # 脱敏：绝不把 API Key 存进 payload
    payload = dict(req.payload or {})
    payload.pop("api_key", None)
    payload.pop("apiKey", None)
    repo = AITaskRepository(db)
    task = repo.create(org_id=org_id, task_type=req.task_type,
                       payload=payload, created_by=req.created_by or "",
                       priority=req.priority)
    runner = RUNNERS[req.task_type]

    def _runner(task_id: str, is_cancelled):
        return runner(task_id, is_cancelled, dict(payload))

    _get_queue().submit(task.id, _runner, priority=req.priority)
    # 重新读一次拿到最新状态
    task = repo.get(task.id)
    assert task is not None
    return _task_out(task)


@router.get("/{task_id}", response_model=AITaskOut)
def get_task(task_id: str,
             org_id: Optional[str] = Query(default=None),
             db: Session = Depends(get_db)):
    """037：查询任务状态 + 结果（完成）/ 进度（进行中）。"""
    repo = AITaskRepository(db)
    # 未传 org_id 时不做租户过滤（MVP 流程）；传了则隔离
    task = repo.get(task_id, org_id) if org_id else repo.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_out(task)


@router.post("/{task_id}/cancel", response_model=AITaskCancelOut)
def cancel_task(task_id: str,
                org_id: Optional[str] = Query(default=None),
                db: Session = Depends(get_db)):
    """038：取消任务。pending 直接取消；running 协作式标记取消。"""
    repo = AITaskRepository(db)
    task = repo.get(task_id, org_id) if org_id else repo.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if is_terminal(task.status):
        return AITaskCancelOut(id=task.id, status=task.status,
                               cancelled=False,
                               message=f"任务已处于终态 {task.status}")
    final_status = _get_queue().cancel(task.id)
    task = repo.get(task.id)
    assert task is not None
    return AITaskCancelOut(
        id=task.id, status=task.status,
        cancelled=task.status == "cancelled",
        message="已取消" if task.status == "cancelled" else "取消请求已提交，"
                "运行中的任务将在检查点停止")


@router.get("", response_model=AITaskListOut)
def list_tasks(org_id: Optional[str] = Query(default=None),
               status: Optional[str] = Query(default=None),
               limit: int = Query(default=50, le=200),
               offset: int = Query(default=0, ge=0),
               db: Session = Depends(get_db)):
    """按组织列出任务（审计/排查用）。"""
    if not org_id:
        raise HTTPException(status_code=400, detail="请指定 org_id")
    repo = AITaskRepository(db)
    tasks = repo.list_by_org(org_id, status=status, limit=limit,
                             offset=offset)
    return AITaskListOut(items=[_task_out(t) for t in tasks],
                         total=len(tasks))
