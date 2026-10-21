"""Phase 4a：Agent HTTP 路由层（薄路由）。

- POST   /api/v1/agent/runs                        创建 agent_run（202）
- GET    /api/v1/agent/runs/{task_id}              查询 run 全状态
- POST   /api/v1/agent/runs/{task_id}/plan         写入/更新执行计划
- POST   /api/v1/agent/runs/{task_id}/confirm      确认等待中的步骤
- POST   /api/v1/agent/runs/{task_id}/cancel       取消 run（复用 ai-tasks 取消语义）
- GET    /api/v1/agent/conversations/{conv_id}/messages  会话消息分页

本层只做 HTTP + 认证（get_current_user）+ 权限（组织成员校验、
404 防枚举）+ 参数校验（Pydantic）；业务逻辑全部委托
backend/services/agent/ 现有函数（create_agent_run / get_run_state /
set_run_plan / confirm_step / list_messages 等）。

响应：统一标准信封（core/response.success → {code:0, data, request_id}）；
错误抛 BusinessError，由 app.py 全局 handler 转标准错误信封，
业务码见 core/error_codes.py。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai_tasks.queue import TaskQueue
from ...ai_tasks.repository import AITaskRepository
from ...ai_tasks.state_machine import is_terminal
from ...auth.database import get_db, get_session_factory
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...rbac import get_membership
from ...repositories.base import EntityNotFound
from ...services.agent import (
    TASK_TYPE_AGENT_RUN,
    Conversation,
    confirm_step,
    count_messages,
    create_agent_run,
    get_run_state,
    list_messages,
    set_run_plan,
    summarize_plan,
)

router = APIRouter(prefix="/agent", tags=["v1-agent"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class AgentRunCreate(BaseModel):
    """创建 agent_run。brief 为用户一句话需求的结构化输入（可选）。"""

    org_id: str = Field(min_length=1, description="组织 id（调用者须为成员）")
    work_id: Optional[str] = Field(default=None, description="关联作品 id（可选，须属该组织）")
    brief: Dict[str, Any] = Field(default_factory=dict)


class AgentRunPlanStep(BaseModel):
    tool_id: str = Field(min_length=1, description="工具 id（须在白名单）")
    params: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[int] = Field(default_factory=list,
                                  description="依赖的步骤索引")


class AgentRunPlan(BaseModel):
    steps: List[AgentRunPlanStep] = Field(default_factory=list)


class AgentRunConfirm(BaseModel):
    step_idx: int = Field(ge=0, description="待确认的步骤索引")
    approved: bool = Field(description="True=批准执行，False=拒绝并取消任务")


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------

def _require_org_member(db: Session, user_id: str,
                        org_id: Optional[str]) -> str:
    """调用者须为该组织成员；不存在/无权一律 404（防组织枚举）。"""
    ms = get_membership(db, user_id, org_id or "")
    if not org_id or ms is None:
        raise BusinessError("组织不存在", code=error_codes.ORG_NOT_FOUND,
                            http_status=404)
    return ms.org_id


def _get_agent_task_or_404(db: Session, task_id: str, user_id: str):
    """取 agent_run 任务：非 agent_run 类型 / 跨组织一律 404（防枚举）。"""
    repo = AITaskRepository(db)
    task = repo.get(task_id)
    if task is None or task.task_type != TASK_TYPE_AGENT_RUN:
        raise BusinessError("任务不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    _require_org_member(db, user_id, task.org_id)
    return task


def _entity_404(exc: EntityNotFound) -> BusinessError:
    """service 层 EntityNotFound → 路由层 404（防枚举口径）。"""
    if exc.entity == "Organization":
        return BusinessError("组织不存在", code=error_codes.ORG_NOT_FOUND,
                             http_status=404)
    if exc.entity == "Work":
        return BusinessError("作品不存在", code=error_codes.WORK_NOT_FOUND,
                             http_status=404)
    return BusinessError("资源不存在", code=error_codes.RESOURCE_NOT_FOUND,
                         http_status=404)


# ---------------------------------------------------------------------------
# run 创建 / 查询
# ---------------------------------------------------------------------------

@router.post("/runs", response_model=ApiResponse[dict], status_code=202)
def create_run(req: AgentRunCreate,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """创建 Agent run：建 AITask(task_type=agent_run) + conversation，202 返回。

    - 须登录；调用者须为 org_id 的成员，否则 404（防枚举）；
    - work_id 非空时必须属于该组织，否则 404；
    - created_by 取服务端登录用户，不信任客户端传入。
    """
    _require_org_member(db, user.id, req.org_id)
    try:
        task_id = create_agent_run(db, user, req.org_id,
                                   work_id=req.work_id,
                                   brief_json=dict(req.brief or {}))
    except EntityNotFound as e:
        raise _entity_404(e) from e
    state = get_run_state(db, task_id, req.org_id)
    return success({
        "task_id": task_id,
        "status": state.get("status"),
        "run_status": state.get("run_status"),
        "conversation_id": state.get("conversation_id"),
        "work_id": state.get("work_id"),
    })


@router.get("/runs/{task_id}", response_model=ApiResponse[dict])
def get_run(task_id: str,
            user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    """查询 run 全状态：task + run_status + steps + 待确认步骤（刷新恢复用）。

    任务必须属于调用者所在组织，否则 404（防枚举/防 IDOR）。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    state = get_run_state(db, task_id, task.org_id)
    if not state.get("found"):
        raise BusinessError("任务不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    state.pop("found", None)
    return success(state)


# ---------------------------------------------------------------------------
# plan / confirm / cancel
# ---------------------------------------------------------------------------

@router.post("/runs/{task_id}/plan", response_model=ApiResponse[dict])
def set_plan(task_id: str,
             req: AgentRunPlan,
             user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """写入/更新 run 的执行计划。

    plan 经 planner 白名单 + params schema + 依赖环 + run 级上限校验；
    非法计划 → 400（INVALID_PARAM），错误明细在 message 中。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    try:
        v = set_run_plan(db, task_id, task.org_id, req.model_dump())
    except EntityNotFound as e:
        raise _entity_404(e) from e
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    return success({"task_id": task_id, **summarize_plan(v)})


@router.post("/runs/{task_id}/confirm", response_model=ApiResponse[dict])
def confirm(task_id: str,
            req: AgentRunConfirm,
            user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    """确认等待中的步骤（waiting_user → 批准后恢复执行 / 拒绝则取消任务）。

    - 非 waiting_user 状态或 step_idx 对不上 → 400；
    - approved=False → 任务 cancelled（用户拒绝）。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    try:
        out = confirm_step(db, task_id, req.step_idx, req.approved,
                           org_id=task.org_id)
    except EntityNotFound as e:
        raise _entity_404(e) from e
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    return success(dict(out))


@router.post("/runs/{task_id}/cancel", response_model=ApiResponse[dict])
def cancel_run(task_id: str,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """取消 run：复用 ai-tasks 取消语义。

    pending 直接取消；running 协作式标记取消（检查点停止）；终态任务幂等返回。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    if is_terminal(task.status):
        return success({
            "task_id": task.id, "status": task.status, "cancelled": False,
            "message": f"任务已处于终态 {task.status}",
        })
    final_status = TaskQueue(get_session_factory()).cancel(task.id)
    return success({
        "task_id": task.id, "status": final_status,
        "cancelled": final_status == "cancelled",
        "message": "已取消" if final_status == "cancelled"
                   else "取消请求已提交，运行中的任务将在检查点停止",
    })


# ---------------------------------------------------------------------------
# 会话消息分页
# ---------------------------------------------------------------------------

@router.get("/conversations/{conv_id}/messages",
            response_model=ApiResponse[dict])
def conv_messages(conv_id: str,
                  limit: int = Query(default=50, ge=1, le=200,
                                     description="每页条数（1–200）"),
                  offset: int = Query(default=0, ge=0,
                                      description="偏移量"),
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """会话消息分页（时间正序）。会话必须属于调用者所在组织，否则 404。"""
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise BusinessError("会话不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    _require_org_member(db, user.id, conv.org_id)
    rows = list_messages(db, conv_id, conv.org_id,
                         limit=limit, offset=offset)
    total = count_messages(db, conv_id, conv.org_id)
    return success({
        "conversation_id": conv_id,
        "items": [{
            "id": m.id,
            "role": m.role,
            "content": m.content_json,
            "created_at": (m.created_at.isoformat()
                           if m.created_at is not None else None),
        } for m in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })
