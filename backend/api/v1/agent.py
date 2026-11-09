"""Phase 4a：Agent HTTP 路由层（薄路由）。

- POST   /api/v1/agent/runs                        创建 agent_run（202）
- GET    /api/v1/agent/runs/{task_id}              查询 run 全状态
- POST   /api/v1/agent/runs/{task_id}/plan         写入/更新执行计划
- POST   /api/v1/agent/runs/{task_id}/confirm      确认等待中的步骤
- POST   /api/v1/agent/runs/{task_id}/cancel       取消 run（复用 ai-tasks 取消语义）
- GET    /api/v1/agent/conversations/{conv_id}/messages  会话消息分页
- POST   /api/v1/agent/one-shot                   一句话生成完整项目（202 入队异步执行 11 步编排）
- POST   /api/v1/agent/modify                     连续对话局部修改（200 同步返回结果）

本层只做 HTTP + 认证（get_current_user）+ 权限（组织成员校验、
404 防枚举）+ 参数校验（Pydantic）；业务逻辑全部委托
backend/services/agent/ 现有函数（create_agent_run / get_run_state /
set_run_plan / confirm_step / list_messages 等）。

响应：统一标准信封（core/response.success → {code:0, data, request_id}）；
错误抛 BusinessError，由 app.py 全局 handler 转标准错误信封，
业务码见 core/error_codes.py。

创建 run 支持 `Idempotency-Key` 请求头（Phase 4a 遗留，Phase 7 补齐）：
同 key + 同请求体 → 直接回放首次创建结果（不重复建任务/会话/扣费）；
同 key + 不同请求体 → 400 幂等键冲突。幂等键走 DB 表（迁移 0021），
多 worker/重启后依然有效。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
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
from ...core.idempotency import idempotency_begin
from ...core.response import ApiResponse, success
from ...rbac import ROLE_ADMIN, ROLE_RANK, get_membership
from ...repositories.base import EntityNotFound
from ...services.agent import (
    TASK_TYPE_AGENT_RUN,
    Conversation,
    check_user_quota,
    confirm_step,
    count_messages,
    create_agent_run,
    get_run_state,
    list_messages,
    pause_run,
    resume_run,
    set_run_plan,
    summarize_plan,
)
from ...services.poster_quota import POSTER_IMAGE_DAILY_LIMIT

router = APIRouter(prefix="/agent", tags=["v1-agent"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class AgentRunCreate(BaseModel):
    """创建 agent_run。brief 为用户一句话需求的结构化输入（可选）。"""

    org_id: str = Field(min_length=1, description="组织 id（调用者须为成员）")
    work_id: Optional[str] = Field(default=None, description="关联作品 id（可选，须属该组织）")
    brief: Dict[str, Any] = Field(default_factory=dict)
    conversation_id: Optional[str] = Field(
        default=None,
        description="复用的会话 id（多轮对话串联；不传则新建会话）")


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


class AgentOneShot(BaseModel):
    """一句话生成完整项目：建 run + 入队异步执行 11 步编排，202 返回 task_id。

    前端用 GET /runs/{task_id} 轮询真实状态（已有接口）。
    """

    org_id: str = Field(min_length=1, description="组织 id（调用者须为成员）")
    brief_text: str = Field(min_length=1, max_length=2000,
                            description="用户一句话需求（自然语言）")
    work_id: Optional[str] = Field(default=None,
                                   description="关联作品 id（可选，须属该组织）")
    conversation_id: Optional[str] = Field(
        default=None, description="复用的会话 id（不传则新建会话）")
    model: Optional[str] = Field(default=None,
                                 description="覆盖默认模型（须在允许列表，由 service 层校验）")


class AgentModify(BaseModel):
    """连续对话中的局部修改：同步执行，200 直接返回结果。

    返回 status=completed（自动执行完毕）/ waiting_user（需确认，
    调 confirm_modification 语义的后续接口）/ failed。
    """

    org_id: str = Field(min_length=1, description="组织 id（调用者须为成员）")
    work_id: str = Field(min_length=1, description="目标作品 id（须属该组织）")
    conversation_id: str = Field(min_length=1, description="会话 id（须绑定该作品）")
    user_text: str = Field(min_length=1, max_length=2000,
                           description="用户修改需求（自然语言）")
    model: Optional[str] = Field(default=None,
                                 description="覆盖默认模型（由 service 层校验）")


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


def _is_org_admin(db: Session, user_id: str, org_id: str) -> bool:
    """该用户在该组织是否为 admin/owner（S2 例外判定用）。"""
    ms = get_membership(db, user_id, org_id)
    return bool(ms) and ROLE_RANK.get(ms.role, 0) >= ROLE_RANK[ROLE_ADMIN]


def _get_agent_task_or_404(db: Session, task_id: str, user_id: str):
    """取 agent_run 任务：非 agent_run 类型 / 跨组织一律 404（防枚举）。

    S2 修复：同组织横向越权防护。仅验组织成员时，同组织任意成员可
    改他人执行计划、批准他人待确认步骤、取消他人任务。现叠加
    created_by 校验：非管理员只能操作自己创建的任务。
    admin/owner 例外是明确设计（组织管理需要查看/管理全组织任务），
    不是遗漏；created_by 为空的老任务（迁移前数据）沿用组织成员口径，
    避免老任务突然不可访问。
    """
    repo = AITaskRepository(db)
    task = repo.get(task_id)
    if task is None or task.task_type != TASK_TYPE_AGENT_RUN:
        raise BusinessError("任务不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    _require_org_member(db, user_id, task.org_id)
    if (getattr(task, "created_by", "") and task.created_by != user_id
            and not _is_org_admin(db, user_id, task.org_id)):
        raise BusinessError("任务不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    return task


def _entity_404(exc: EntityNotFound) -> BusinessError:
    """service 层 EntityNotFound → 路由层 404（防枚举口径）。"""
    if exc.entity == "Organization":
        return BusinessError("组织不存在", code=error_codes.ORG_NOT_FOUND,
                             http_status=404)
    if exc.entity == "Work":
        return BusinessError("作品不存在", code=error_codes.WORK_NOT_FOUND,
                             http_status=404)
    if exc.entity == "Conversation":
        return BusinessError("会话不存在",
                             code=error_codes.RESOURCE_NOT_FOUND,
                             http_status=404)
    return BusinessError("资源不存在", code=error_codes.RESOURCE_NOT_FOUND,
                         http_status=404)


def _enqueue_run_resume(task) -> None:
    """confirm approved 后：把 run 重新入队，由 worker 异步恢复执行。

    Phase 5（4a 遗留修复）：不在 HTTP 请求内同步跑完剩余步骤。
    模块级函数，便于测试 monkeypatch 拦截入队行为。
    """
    # 函数内导入：避免 api.v1.agent ↔ ai_tasks.runners 顶层循环导入
    from ...ai_tasks.runners import RUNNERS
    runner = RUNNERS[TASK_TYPE_AGENT_RUN]

    def _runner(task_id: str, is_cancelled):
        return runner(task_id, is_cancelled, {})

    TaskQueue(get_session_factory()).submit(
        task.id, _runner, priority=task.priority or "high")


# ---------------------------------------------------------------------------
# run 创建 / 查询
# ---------------------------------------------------------------------------

@router.get("/quota", response_model=ApiResponse[dict])
def agent_quota(org_id: str = Query(...),
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Agent 侧今日配图额度（与 POST /poster/image 同一口径）。

    用途：Agent 面板/助手判断「今天还能不能出海报配图」，避免承诺做不到的图。
    只读，不扣额度；真正拦截在生图端点（超限 429）。
    """
    org = _require_org_member(db, user.id, org_id)
    ms = get_membership(db, user.id, org)
    q = check_user_quota(user_id=user.id, org_id=org, db=db,
                         role=ms.role if ms else None)
    q["daily_limit"] = POSTER_IMAGE_DAILY_LIMIT
    return success(q)


@router.post("/runs", response_model=ApiResponse[dict], status_code=202)
def create_run(req: AgentRunCreate,
               request: Request,
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """创建 Agent run：建 AITask(task_type=agent_run) + conversation，202 返回。

    - 须登录；调用者须为 org_id 的成员，否则 404（防枚举）；
    - work_id 非空时必须属于该组织，否则 404；
    - created_by 取服务端登录用户，不信任客户端传入；
    - 支持 `Idempotency-Key`（Phase 7 DB 版）：同 key 同体直接回放首次
      创建结果（不重复建任务/会话），同 key 不同体 → 400 幂等键冲突。
    """
    idem = idempotency_begin("agent-runs", request,
                            req.model_dump(mode="json"),
                            scope=req.org_id, org_id=req.org_id,
                            user_id=user.id)
    if idem.replay is not None:
        return success(dict(idem.replay))
    try:
        _require_org_member(db, user.id, req.org_id)
        try:
            task_id = create_agent_run(db, user, req.org_id,
                                       work_id=req.work_id,
                                       brief_json=dict(req.brief or {}),
                                       conversation_id=req.conversation_id)
        except EntityNotFound as e:
            raise _entity_404(e) from e
        state = get_run_state(db, task_id, req.org_id)
    except Exception:
        # 失败不留脏记录：等待者/重试可重新执行
        idem.abort()
        raise
    data = {
        "task_id": task_id,
        "status": state.get("status"),
        "run_status": state.get("run_status"),
        "conversation_id": state.get("conversation_id"),
        "work_id": state.get("work_id"),
    }
    return success(idem.finish(data))


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
    """确认等待中的步骤（waiting_user → 批准/拒绝）。

    - 非 waiting_user 状态或 step_idx 对不上 → 400；
    - approved=False → 任务 cancelled（用户拒绝）；
    - approved=True → 只落确认态（Phase 5 改异步），随后把任务重新入队，
      由 worker 异步恢复执行；HTTP 直接返回 running，前端轮询真实进度。
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
    if req.approved and out.get("run_status") == "running":
        # 4a 遗留修复：批准后只入队，不在请求内同步执行剩余步骤
        _enqueue_run_resume(task)
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


@router.post("/runs/{task_id}/pause", response_model=ApiResponse[dict])
def pause(task_id: str,
          user: User = Depends(get_current_user),
          db: Session = Depends(get_db)):
    """暂停 run：只置 paused 标记（幂等），runner 在步骤边界协作式挂起。

    - 终态任务或 run_status 非 queued/running/paused → 400；
    - 暂停是协作式的：正在执行的步骤会跑完，在下个步骤边界挂起。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    try:
        out = pause_run(db, task_id, org_id=task.org_id)
    except EntityNotFound as e:
        raise _entity_404(e) from e
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    return success(dict(out))


@router.post("/runs/{task_id}/resume", response_model=ApiResponse[dict])
def resume(task_id: str,
           user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """继续 run：清 paused 标记。

    - 未暂停 → 400；
    - runner 已在边界挂起（run_status=paused）→ 重新入队恢复执行；
    - worker 还没走到暂停边界 → 只清标记，worker 自然继续（不重入队，
      避免双 worker 并发）。
    """
    task = _get_agent_task_or_404(db, task_id, user.id)
    try:
        out = resume_run(db, task_id, org_id=task.org_id)
    except EntityNotFound as e:
        raise _entity_404(e) from e
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    if out.get("requeue"):
        _enqueue_run_resume(task)
    return success(dict(out))


# ---------------------------------------------------------------------------
# 一句话生成 / 局部修改（service 层已完备，本层只做 HTTP 薄封装）
# ---------------------------------------------------------------------------

@router.post("/one-shot", response_model=ApiResponse[dict], status_code=202)
def one_shot(req: AgentOneShot,
             request: Request,
             user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """一句话生成完整项目：建 run + 入队异步执行 11 步编排，202 返回。

    - 须登录；调用者须为 org_id 的成员，否则 404（防枚举）；
    - work_id 非空时必须属于该组织，否则 404；
    - 支持 `Idempotency-Key`（DB 版）：同 key 同体直接回放，不重复建任务/执行；
    - 执行走 TaskQueue worker（与 _enqueue_run_resume 同一入队方式），
      前端用 GET /runs/{task_id} 轮询真实状态。
    """
    # 函数内导入：避免 api.v1.agent ↔ services.agent.orchestrator 顶层循环导入
    from ...services.agent.orchestrator import run_one_shot_brief

    idem = idempotency_begin("agent-one-shot", request,
                            req.model_dump(mode="json"),
                            scope=req.org_id, org_id=req.org_id,
                            user_id=user.id)
    if idem.replay is not None:
        return success(dict(idem.replay))
    try:
        _require_org_member(db, user.id, req.org_id)
        try:
            task_id = create_agent_run(
                db, user, req.org_id, work_id=req.work_id,
                brief_json={"text": req.brief_text[:2000]},
                conversation_id=req.conversation_id)
        except EntityNotFound as e:
            raise _entity_404(e) from e

        user_id, org_id = user.id, req.org_id
        brief_text, work_id = req.brief_text, req.work_id
        conversation_id, model = req.conversation_id, req.model

        def _runner(tid: str, is_cancelled):
            db2 = get_session_factory()()
            try:
                return run_one_shot_brief(
                    db2, user_id, org_id, brief_text, task_id=tid,
                    work_id=work_id, conversation_id=conversation_id,
                    model=model, is_cancelled=is_cancelled)
            finally:
                db2.close()

        # 与 _enqueue_run_resume 同一入队方式
        TaskQueue(get_session_factory()).submit(task_id, _runner,
                                                priority="high")
        state = get_run_state(db, task_id, req.org_id)
    except Exception:
        idem.abort()
        raise
    data = {
        "task_id": task_id,
        "status": state.get("status"),
        "run_status": state.get("run_status"),
        "conversation_id": state.get("conversation_id"),
        "work_id": state.get("work_id"),
    }
    return success(idem.finish(data))


@router.post("/modify", response_model=ApiResponse[dict])
def modify(req: AgentModify,
           user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """连续对话中的局部修改（任务书 §十）：同步执行，200 直接返回结果。

    - 须登录；调用者须为 org_id 的成员，否则 404（防枚举）；
    - 会话必须绑定目标作品，否则 404（跨作品拒绝，由 service 层校验）；
    - ModifyParseError → 400；ModifyExecutionError → 500；EntityNotFound → 404。
    同步接口已有先例（colloquial/ideas 同步改写），单次修改通常 1–2 次模型调用。
    """
    # 函数内导入：避免顶层循环导入
    from ...services.agent.modify import (
        ModifyExecutionError,
        ModifyParseError,
        apply_modification,
    )

    _require_org_member(db, user.id, req.org_id)
    try:
        result = apply_modification(
            db, user, req.org_id, req.work_id, req.conversation_id,
            req.user_text, model=req.model)
    except EntityNotFound as e:
        raise _entity_404(e) from e
    except ModifyParseError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    except ModifyExecutionError as e:
        raise BusinessError(f"局部修改执行失败：{e}",
                            code=error_codes.SYSTEM_ERROR,
                            http_status=500) from e
    return success(result)


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
    """会话消息分页（时间正序）。会话必须属于调用者所在组织，否则 404。

    S2 修复：叠加会话归属校验。仅验组织成员时，同组织成员可读他人
    会话全文。现要求会话 user_id == 调用者，或调用者为 admin/owner。
    """
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise BusinessError("会话不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    _require_org_member(db, user.id, conv.org_id)
    if (conv.user_id != user.id
            and not _is_org_admin(db, user.id, conv.org_id)):
        raise BusinessError("会话不存在",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
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
