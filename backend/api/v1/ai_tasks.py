"""AI 异步任务 API（034/037/038）。

- POST   /api/v1/ai-tasks                  创建任务 → {task_id, status}
- GET    /api/v1/ai-tasks/{task_id}        查询状态/结果（037）
- POST   /api/v1/ai-tasks/{task_id}/cancel 取消任务（038）
- GET    /api/v1/ai-tasks                  按组织列出任务（可选 ?status=）

认证：V3.1 P0 起强制登录。org_id 必传且调用者须为该组织成员
（不存在/无权一律 404，防止组织枚举）。创建接口支持
Idempotency-Key 去重，runner 经 payload._org_id 拿到组织，
预算/用量正常生效。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...ai_tasks.queue import TaskQueue, sweep_timed_out
from ...ai_tasks.repository import AITaskRepository
from ...ai_tasks.runners import RUNNERS
from ...ai_tasks.state_machine import is_terminal
from ...auth.database import get_db, get_session_factory
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core.idempotency import idempotency_begin
from ...models import TASK_TYPES, Organization
from ...rbac import get_membership
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
    """MVP 兼容：无 org_id 时使用内置 default 组织（自动建）。

    注意：/api/v1/ai-tasks 建任务接口 V3.1 起强制登录 + 必传 org_id，
    不再走此分支；仅 ideas 等同步接口的产品事实 grounding 等
    旧链路仍可使用。
    """
    org = db.scalar(
        select(Organization).where(Organization.name == DEFAULT_ORG_NAME))
    if org is None:
        org = Organization(name=DEFAULT_ORG_NAME, created_by="system")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org


def _resolve_org_id(db: Session, org_id: Optional[str]) -> str:
    """旧链路兼容：有 org_id 则校验存在（404），无则用 default 组织。"""
    if org_id:
        org = db.scalar(
            select(Organization).where(Organization.id == org_id))
        if org is None:
            raise HTTPException(status_code=404, detail="组织不存在")
        return org.id
    return _ensure_default_org(db).id


def _require_org_member(db: Session, user_id: str,
                        org_id: Optional[str]) -> str:
    """校验调用者为该组织成员；不存在/无权一律 404（防组织枚举）。

    返回确认存在的 org_id。
    """
    ms = get_membership(db, user_id, org_id or "")
    if not org_id or ms is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    return ms.org_id


def _is_org_admin(db: Session, user_id: str, org_id: str) -> bool:
    """该用户在该组织是否为 admin/owner（S-1 例外判定用，与 agent.py 同口径）。"""
    from ...rbac import ROLE_ADMIN, ROLE_RANK
    ms = get_membership(db, user_id, org_id)
    return bool(ms) and ROLE_RANK.get(ms.role, 0) >= ROLE_RANK[ROLE_ADMIN]


def _get_task_or_404(db: Session, task_id: str, user_id: str):
    """S-1 修复：通用任务路由的 created_by 校验（与 agent.py 的
    _get_agent_task_or_404 同标准，类型不过滤——通用路由本就管理全类型任务）。

    只验组织成员时，同组织任意成员可读他人 agent_run 结果、取消他人
    运行中任务，agent.py 的 created_by 校验被完全绕过。现叠加
    created_by：非管理员只能操作自己创建的任务。
    admin/owner 例外是明确设计（组织管理需要查看/管理全组织任务），
    不是遗漏；created_by 为空的老任务（迁移前数据）沿用组织成员口径，
    避免老任务突然不可访问。
    """
    repo = AITaskRepository(db)
    task = repo.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _require_org_member(db, user_id, task.org_id)
    if (getattr(task, "created_by", "") and task.created_by != user_id
            and not _is_org_admin(db, user_id, task.org_id)):
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


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
def create_task(req: AITaskCreate, request: Request,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """034：创建 AI 异步任务，返回 task_id（202 Accepted）。

    payload 与同步接口请求体结构一致。注意：异步任务只用服务端
    配置的 AI（环境变量），请求头自带 Key 的请走同步接口。

    V3.1 P0：强制登录；org_id 必传且调用者须为成员（404 防枚举）；
    支持 Idempotency-Key（同 key 同体直接回放，不重复建任务扣费）；
    created_by 取服务端登录用户，不再信任客户端传入。
    """
    if req.task_type not in TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的任务类型: {req.task_type}，可选 {list(TASK_TYPES)}")
    if req.task_type not in RUNNERS:
        raise HTTPException(
            status_code=400,
            detail=f"任务类型 {req.task_type} 暂不支持异步执行")
    org_id = _require_org_member(db, user.id, req.org_id)
    # V3.1 P0：幂等——双击/重试只建一个任务
    # S-2 修复：补 user_id，幂等键按用户隔离（同组织不同用户同 key 不再互相回放）
    idem = idempotency_begin("ai-tasks", request, req.model_dump(),
                             scope=org_id, user_id=user.id)
    if idem.replay is not None:
        replay = dict(idem.replay)
        replay["idempotent_replay"] = True  # V3.1 P0：让前端可识别幂等回放
        return replay
    try:
        # 脱敏：绝不把 API Key 存进 payload
        payload = dict(req.payload or {})
        payload.pop("api_key", None)
        payload.pop("apiKey", None)
        # S6.1：注入 org_id，供 runner 在 gateway 调用前做事实引用检查与预算
        payload["_org_id"] = org_id
        repo = AITaskRepository(db)
        task = repo.create(org_id=org_id, task_type=req.task_type,
                           payload=payload, created_by=user.id,
                           priority=req.priority)
        runner = RUNNERS[req.task_type]

        def _runner(task_id: str, is_cancelled):
            return runner(task_id, is_cancelled, dict(payload))

        _get_queue().submit(task.id, _runner, priority=req.priority)
        # 重新读一次拿到最新状态
        task = repo.get(task.id)
        assert task is not None
        return idem.finish(_task_out(task).model_dump(mode="json"))
    except Exception:
        idem.abort()
        raise


@router.get("/{task_id}", response_model=AITaskOut)
def get_task(task_id: str,
             user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """037：查询任务状态 + 结果（完成）/ 进度（进行中）。

    V3.1 P1-6：租户隔离——任务必须属于调用者所在组织，否则 404。

    S-1：叠加 created_by 校验（见 _get_task_or_404），否则通用路由
    会绕过 agent.py 的归属校验。

    V3.1 P1-4：查询前做一次惰性超时扫描——前端每 2.5s 轮询一次，
    超时任务会在下次轮询时被标记为 failed，前端即停止等待。
    """
    import logging
    try:
        sweep_timed_out(get_session_factory())
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("超时扫描失败", exc_info=True)
    task = _get_task_or_404(db, task_id, user.id)
    return _task_out(task)


@router.post("/{task_id}/cancel", response_model=AITaskCancelOut)
def cancel_task(task_id: str,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """038：取消任务。pending 直接取消；running 协作式标记取消。

    V3.1 P1-6：租户隔离同上。
    S-1：叠加 created_by 校验（见 _get_task_or_404）——取消属破坏性
    操作，绝不能让同组织任意成员取消他人任务。
    """
    task = _get_task_or_404(db, task_id, user.id)
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
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """按组织列出任务（审计/排查用）。

    S-1：普通成员只能列自己创建的任务（admin/owner 可列全组织）。
    否则列表本身就是枚举口：拿到他人 task_id 后结合原先无 created_by
    的 get/cancel 即可读/取消他人任务，形成“枚举→读取/取消”闭环。
    """
    if not org_id:
        raise HTTPException(status_code=400, detail="请指定 org_id")
    _require_org_member(db, user.id, org_id)
    repo = AITaskRepository(db)
    created_by = None if _is_org_admin(db, user.id, org_id) else user.id
    tasks = repo.list_by_org(org_id, status=status, limit=limit,
                             offset=offset, created_by=created_by)
    return AITaskListOut(items=[_task_out(t) for t in tasks],
                         total=len(tasks))
