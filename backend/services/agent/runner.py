"""Phase 3：Agent run 生命周期。

run 建模为 AITask(task_type="agent_run")（PHASE1_DESIGN.md §2/§4 结论）：
- 不改 TASK_STATUS 表；waiting_user / partial 用 payload_json.run_status
  扩展字段表达（running+waiting_user / succeeded+partial）；
- step 执行记录写 agent_steps；去重键 (task_id, tool_id, params_digest)；
- 取消复用 cancel_requested 协作式取消，取消后状态为 cancelled，
  绝不误报 completed；
- 同一 run 内的写操作严格按 plan 顺序串行执行；跨 run 的并发写冲突
  由 Work.version 乐观锁 + adapter 的 conflict 错误码暴露（不静默覆盖）。

payload_json 布局::

    {
      "brief": {...},            # 用户目标结构化摘要（脱敏）
      "plan": {"steps": [...]},  # validate_plan 校验过的原始步骤
      "run_status": "queued|running|waiting_user|paused|partial|completed|failed|cancelled",
      "work_id": ...,
      "conversation_id": ...,
      "pending_step": {"step_idx":..,"tool_id":..,"params_digest":..} | None,
      "confirmed_steps": [0, 2],  # 已确认的步骤索引
      "summary": {...},           # 终态摘要
    }

执行入口有两条（共用 execute_run_steps，无状态丢失）：
- run_agent_run(task_id, is_cancelled, payload)：queue runner 签名，
  由 ai_tasks RUNNERS 注册；waiting_user 时返回挂起信号，
  queue._execute 识别后保持 running 不落终态；
- confirm_step(...)：用户确认后**只落确认态**（Phase 5 改异步），
  由路由层重新入队、worker（run_agent_run）异步恢复执行；
  刷新页面后重读 task 即恢复。
"""
from __future__ import annotations

# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入，先走 backend.auth.*。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ...models import AITask, Membership, Work
from ...repositories.base import EntityNotFound
from ...ai_tasks.repository import AITaskRepository
from .adapters import ToolDeps, ToolResult, run_tool
from .brief import merge_brief_dict
from .context import AgentIdentity
from .conversations import (
    AgentStep,
    append_message,
    create_conversation,
    get_conversation,
)
from .planner import PlanStep, PlanValidation, validate_plan
from .tools import ToolTier, get_tool

logger = logging.getLogger(__name__)

__all__ = [
    "TASK_TYPE_AGENT_RUN",
    "RUN_STATUSES",
    "RUN_STATUS_QUEUED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_WAITING_USER",
    "RUN_STATUS_PARTIAL",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_CANCELLED",
    "create_agent_run",
    "set_run_plan",
    "get_run_state",
    "run_agent_run",
    "execute_run_steps",
    "confirm_step",
    "step_params_digest",
    "STEP_TIMEOUT_SECONDS",
    "MAX_RUN_TOTAL_SECONDS",
]

TASK_TYPE_AGENT_RUN = "agent_run"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_USER = "waiting_user"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUSES = (
    RUN_STATUS_QUEUED, RUN_STATUS_RUNNING, RUN_STATUS_WAITING_USER,
    RUN_STATUS_PAUSED,
    RUN_STATUS_PARTIAL, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
)

# agent_steps.status 取值
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_WAITING_USER = "waiting_user"
STEP_CANCELLED = "cancelled"
STEP_SKIPPED = "skipped"

# 挂起信号：queue._execute 见到此 run_status 保持 running，不落终态
_SUSPENDED = RUN_STATUS_WAITING_USER

#: Phase 7 上限 enforcement（任务书 §7.2 / §十一）：
#: - 单步执行超时（秒）：一步工具调用超过此时长 → 该步记 step_timeout 失败
#:   （可选步骤 → partial；必需步骤 → failed；已有成果保留）
#: - run 总执行时间上限（秒）：超时后不再启动新步骤 → partial 并保留成果
#: run 级步数/模型步数上限见 planner.MAX_PLAN_STEPS / MAX_MODEL_STEPS_PER_RUN
STEP_TIMEOUT_SECONDS = 5 * 60
MAX_RUN_TOTAL_SECONDS = 30 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def step_params_digest(params: dict[str, Any]) -> str:
    """步骤去重键的 params 部分：canonical JSON 的 sha256。"""
    canon = json.dumps(params or {}, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _normalize_user_id(user: Any) -> str:
    if isinstance(user, str):
        return user
    for attr in ("user_id", "id"):
        v = getattr(user, attr, None)
        if v:
            return str(v)
    raise ValueError("无法从 user 解析 user_id")


def _resolve_role(db: Session, user_id: str, org_id: str) -> str:
    ms = (db.query(Membership)
            .filter_by(user_id=user_id, org_id=org_id).first())
    return ms.role if ms else "viewer"


# ---------------------------------------------------------------------------
# run 创建
# ---------------------------------------------------------------------------

def create_agent_run(db: Session, user: Any, org_id: str,
                     work_id: Optional[str] = None,
                     brief_json: Optional[dict[str, Any]] = None,
                     conversation_id: Optional[str] = None) -> str:
    """创建 Agent run：AITask(task_type="agent_run") + conversation 行。

    - 用户须是 org 成员，否则 EntityNotFound（404 防枚举）；
    - work_id 非空时必须属于该 org，否则 EntityNotFound（防跨作品）；
    - conversation_id 非空时复用同一会话（多轮对话串联，4b 遗留③）：
      会话不存在/不属于该 org → EntityNotFound；复用时把新 brief 增量合并
      进会话 brief_json，不新建 conversation；
    - payload.run_status 初始为 queued；plan 为 None（Phase 5 前由
      set_run_plan 写入，或测试直接写）。
    返回 task_id。202 语义由路由层（Phase 4）接。
    """
    user_id = _normalize_user_id(user)
    ms = (db.query(Membership)
            .filter_by(user_id=user_id, org_id=org_id).first())
    if ms is None:
        raise EntityNotFound("Organization", org_id)
    work = None
    if work_id:
        work = (db.query(Work)
                  .filter(Work.id == work_id, Work.org_id == org_id)
                  .first())
        if work is None:
            raise EntityNotFound("Work", work_id)

    if conversation_id:
        conv = get_conversation(db, conversation_id, org_id)
        if conv is None:
            raise EntityNotFound("Conversation", conversation_id)
        # brief 增量更新：新值补齐/覆盖旧值，不丢历史上下文
        conv.brief_json = merge_brief_dict(
            dict(conv.brief_json or {}),
            {"brief": dict(brief_json or {}),
             "work_title": work.title if work else None})
        db.commit()
    else:
        conv = create_conversation(
            db, org_id, user_id, work_id=work_id,
            brief={"brief": dict(brief_json or {}),
                   "work_title": work.title if work else None},
        )
    if brief_json:
        append_message(db, conv.id, org_id, "user",
                       {"type": "brief", "brief": dict(brief_json)})

    repo = AITaskRepository(db)
    task = repo.create(
        org_id=org_id, task_type=TASK_TYPE_AGENT_RUN,
        payload={
            "brief": dict(brief_json or {}),
            "plan": None,
            "run_status": RUN_STATUS_QUEUED,
            "work_id": work_id,
            "conversation_id": conv.id,
            "pending_step": None,
            "confirmed_steps": [],
            "_org_id": org_id,
        },
        created_by=user_id, priority="high",
    )
    logger.info("创建 agent_run task=%s conv=%s org=%s work=%s",
                task.id, conv.id, org_id, work_id)
    return task.id


def set_run_plan(db: Session, task_id: str, org_id: str,
                 plan: dict[str, Any]) -> PlanValidation:
    """写入/更新 run 的执行计划（validate_plan 校验）。

    非法计划抛 ValueError（附 errors）；成功返回 PlanValidation。
    写入后 run_status 保持 queued（未开始）或 running（恢复场景不动）。
    """
    repo = AITaskRepository(db)
    task = repo.get(task_id, org_id)
    if task is None or task.task_type != TASK_TYPE_AGENT_RUN:
        raise EntityNotFound("AITask", task_id)
    v = validate_plan(plan)
    if not v.ok:
        raise ValueError(f"非法 plan：{'; '.join(v.errors)}")
    payload = dict(task.payload_json or {})
    payload["plan"] = {"steps": [dict(s) for s in
                                 (plan.get("steps") or [])]}
    repo.update_payload(task_id, payload)
    return v


def get_run_state(db: Session, task_id: str,
                  org_id: Optional[str] = None) -> dict[str, Any]:
    """读 run 全状态（刷新恢复用）：task + run_status + steps + 待确认步骤。

    org_id 非空时做租户隔离；不匹配返回 {"found": False}（404 防枚举）。
    """
    repo = AITaskRepository(db)
    task = repo.get(task_id, org_id)
    if task is None or task.task_type != TASK_TYPE_AGENT_RUN:
        return {"found": False}
    payload = dict(task.payload_json or {})
    steps = (db.query(AgentStep)
               .filter(AgentStep.task_id == task_id)
               .order_by(AgentStep.started_at.asc().nullsfirst(),
                         AgentStep.id.asc())
               .all())
    return {
        "found": True,
        "task_id": task.id,
        "status": task.status,
        "run_status": payload.get("run_status"),
        "progress": task.progress,
        "work_id": payload.get("work_id"),
        "conversation_id": payload.get("conversation_id"),
        "pending_step": payload.get("pending_step"),
        "confirmed_steps": payload.get("confirmed_steps") or [],
        "error": task.error,
        "summary": payload.get("summary"),
        "steps": [
            {"tool_id": s.tool_id, "status": s.status,
             "output_ref": s.output_ref, "error": s.error,
             "duration_ms": s.duration_ms}
            for s in steps
        ],
    }


# ---------------------------------------------------------------------------
# 步骤执行
# ---------------------------------------------------------------------------

def _step_row(db: Session, task_id: str, tool_id: str,
              digest: str) -> AgentStep:
    row = AgentStep(task_id=task_id, tool_id=tool_id,
                    params_digest=digest, status=STEP_PENDING)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _already_succeeded(db: Session, task_id: str, tool_id: str,
                       digest: str) -> bool:
    """去重：同 (task_id, tool_id, params_digest) 已有 succeeded 行则跳过。"""
    return (db.query(AgentStep)
              .filter(AgentStep.task_id == task_id,
                      AgentStep.tool_id == tool_id,
                      AgentStep.params_digest == digest,
                      AgentStep.status == STEP_SUCCEEDED)
              .first() is not None)


def _step_needs_confirmation(raw_step: dict[str, Any],
                             needs_idx: set[int], idx: int) -> bool:
    """该步是否需要用户确认：planner 标记 / 步骤显式标记 / 工具分级。"""
    if idx in needs_idx:
        return True
    if raw_step.get("needs_confirmation"):
        return True
    defn = get_tool(raw_step.get("tool_id") or "")
    return defn is not None and defn.tier == ToolTier.USER_CONFIRMATION_REQUIRED


def _load_task_fresh(db: Session, task_id: str,
                     org_id: Optional[str] = None) -> AITask:
    repo = AITaskRepository(db)
    task = repo.get(task_id, org_id)
    if task is None or task.task_type != TASK_TYPE_AGENT_RUN:
        raise EntityNotFound("AITask", task_id)
    return task


def execute_run_steps(db: Session, task_id: str,
                      is_cancelled: Callable[[], bool],
                      run_tool_fn: Optional[Callable] = None) -> dict[str, Any]:
    """步骤循环（queue runner 与 confirm_step 共用；无状态丢失）。

    - pending → running（queue 已置 running 时跳过）；
    - 按 plan.steps 顺序串行执行，每步写 agent_steps；
    - 取消：检测到 is_cancelled() → 落 cancelled 并抛 _TaskCancelled
     （queue 协议；confirm_step 路径会接住转返回值）；
    - waiting_user：返回 {"run_status": "waiting_user", ...} 挂起信号；
    - 可选步骤失败 → partial；必需步骤失败 → failed；
    - 全部成功 → succeeded + run_status=completed。
    """
    from ...ai_tasks.queue import _TaskCancelled

    fn = run_tool_fn or run_tool
    repo = AITaskRepository(db)
    task = _load_task_fresh(db, task_id)
    org_id = task.org_id
    payload = dict(task.payload_json or {})

    if task.status == "pending":
        repo.set_status(task_id, "running")
        task = _load_task_fresh(db, task_id)
        payload = dict(task.payload_json or {})
    elif task.status != "running":
        # 终态：无事可做（刷新/重复调用幂等）
        return {"run_status": payload.get("run_status"),
                "task_id": task_id, "note": "already_terminal"}

    payload["run_status"] = RUN_STATUS_RUNNING
    repo.update_payload(task_id, payload)

    plan = payload.get("plan") or {}
    raw_steps: list[dict[str, Any]] = plan.get("steps") or []
    if not raw_steps:
        err = "缺少执行计划（plan 为空），run 无法执行"
        payload["run_status"] = RUN_STATUS_FAILED
        repo.update_payload(task_id, payload)
        repo.set_status(task_id, "failed", error=err)
        return {"run_status": RUN_STATUS_FAILED, "task_id": task_id,
                "error": err}

    v: PlanValidation = validate_plan({"steps": raw_steps})
    if not v.ok:
        err = f"plan 非法：{'; '.join(v.errors)}"
        payload["run_status"] = RUN_STATUS_FAILED
        repo.update_payload(task_id, payload)
        repo.set_status(task_id, "failed", error=err)
        return {"run_status": RUN_STATUS_FAILED, "task_id": task_id,
                "error": err}

    needs_idx = set(v.needs_confirmation)
    confirmed: set[int] = set(payload.get("confirmed_steps") or [])
    conv_id = payload.get("conversation_id")
    user_id = task.created_by
    deps = ToolDeps(
        db=db,
        identity=AgentIdentity(user_id=user_id, org_id=org_id,
                              role=_resolve_role(db, user_id, org_id)),
        ai_generate=None,  # Phase 3：需模型的工具走 needs_model 结构化失败
    )

    n = len(v.steps)
    partial = False
    dedup_skipped = 0
    failed_idx: Optional[int] = None
    failed_error: Optional[str] = None
    succeeded_steps = 0
    t_run_start = _utcnow()

    def _check_cancel() -> None:
        if is_cancelled():
            payload["run_status"] = RUN_STATUS_CANCELLED
            repo.update_payload(task_id, payload)
            if conv_id:
                append_message(db, conv_id, org_id, "assistant",
                               {"type": "run_cancelled",
                                "task_id": task_id})
            raise _TaskCancelled()

    def _check_pause() -> Optional[dict[str, Any]]:
        """用户暂停检查（步骤边界协作式）。

        用 fresh 读 + fresh 写，避免覆盖 pause 接口并发写入的 paused 标记。
        命中则落 run_status=paused 并返回挂起信号（queue 保持 running 不落终态，
        与 waiting_user 同机制；resume 时由路由层重新入队恢复）。
        """
        fresh = _load_task_fresh(db, task_id)
        p = dict(fresh.payload_json or {})
        if not p.get("paused"):
            return None
        p["run_status"] = RUN_STATUS_PAUSED
        repo.update_payload(task_id, p)
        if conv_id:
            append_message(db, conv_id, org_id, "assistant",
                           {"type": "run_paused", "task_id": task_id})
        logger.info("agent_run %s 用户暂停，在步骤边界挂起", task_id)
        return {"run_status": RUN_STATUS_PAUSED, "task_id": task_id,
                "note": "paused_by_user"}

    def _land_time_budget_partial() -> dict[str, Any]:
        """总执行时间超限：保留已有成果进 partial，不再启动新步骤。"""
        fresh = _load_task_fresh(db, task_id)
        p = dict(fresh.payload_json or {})
        p["run_status"] = RUN_STATUS_PARTIAL
        p["summary"] = {"partial": True,
                        "time_budget_exceeded": True,
                        "budget_seconds": MAX_RUN_TOTAL_SECONDS,
                        "succeeded_steps": succeeded_steps,
                        "dedup_skipped": dedup_skipped,
                        "step_count": n}
        repo.update_payload(task_id, p)
        repo.set_status(task_id, "succeeded", progress=100,
                        result={"run_status": RUN_STATUS_PARTIAL,
                                "summary": p["summary"]})
        if conv_id:
            append_message(db, conv_id, org_id, "assistant",
                           {"type": "run_done", "task_id": task_id,
                            "run_status": RUN_STATUS_PARTIAL,
                            "summary": p["summary"]})
        logger.warning("agent_run %s 总执行时间超限（>%ss）：%d/%d 步完成，"
                       "进 partial 保留成果",
                       task_id, MAX_RUN_TOTAL_SECONDS, succeeded_steps, n)
        return {"run_status": RUN_STATUS_PARTIAL, "task_id": task_id,
                "summary": p["summary"]}

    for s in v.steps:
        raw = raw_steps[s.index] if s.index < len(raw_steps) else {}
        optional = bool(raw.get("optional", False))
        digest = step_params_digest(s.params)
        _check_cancel()
        _suspended = _check_pause()
        if _suspended is not None:
            return _suspended

        # Phase 7：总执行时间上限——超时后不再启动新步骤
        elapsed = (_utcnow() - t_run_start).total_seconds()
        if elapsed > MAX_RUN_TOTAL_SECONDS:
            return _land_time_budget_partial()

        # 去重：已成功执行过的不重复执行（重复提交/恢复幂等）
        if _already_succeeded(db, task_id, s.tool_id, digest):
            dedup_skipped += 1
            continue

        # 确认门：需确认且未确认 → 挂起等待用户
        if _step_needs_confirmation(raw, needs_idx, s.index) \
                and s.index not in confirmed:
            row = _step_row(db, task_id, s.tool_id, digest)
            row.status = STEP_WAITING_USER
            row.started_at = _utcnow()
            db.commit()
            payload["run_status"] = RUN_STATUS_WAITING_USER
            payload["pending_step"] = {
                "step_idx": s.index, "tool_id": s.tool_id,
                "params_digest": digest,
                "optional": optional,
                "requested_at": _utcnow().isoformat(),
            }
            repo.update_payload(task_id, payload)
            repo.update_progress(
                task_id, int(s.index / n * 100) if n else 0)
            if conv_id:
                append_message(db, conv_id, org_id, "assistant",
                               {"type": "waiting_user",
                                "task_id": task_id,
                                "step_idx": s.index,
                                "tool_id": s.tool_id})
            logger.info("agent_run %s 挂起等待确认 step=%d tool=%s",
                        task_id, s.index, s.tool_id)
            return {"run_status": _SUSPENDED, "task_id": task_id,
                    "pending_step": payload["pending_step"]}

        # 执行一步
        row = _step_row(db, task_id, s.tool_id, digest)
        row.status = STEP_RUNNING
        row.started_at = _utcnow()
        db.commit()
        t0 = _utcnow()
        try:
            result: ToolResult = fn(deps, s.tool_id, s.params,
                                    confirmed=(s.index in confirmed))
        except Exception as e:  # noqa: BLE001  run_tool 内部已结构化；兜底
            result = ToolResult(status="failed", error_code="internal",
                                error_message=f"{type(e).__name__}: {e}")
        dt_ms = int((_utcnow() - t0).total_seconds() * 1000)
        # Phase 7：单步超时——超过 STEP_TIMEOUT_SECONDS 的一步记失败，
        # 即使工具本身返回了 ok（防单个工具拖住整个 run）
        if dt_ms > STEP_TIMEOUT_SECONDS * 1000:
            result = ToolResult(
                status="failed", error_code="step_timeout",
                error_message=f"单步执行超时（>{STEP_TIMEOUT_SECONDS}s）")
        ok = result.status == "ok"
        row.status = STEP_SUCCEEDED if ok else STEP_FAILED
        row.output_ref = result.artifact_ref
        row.error = result.error_message or result.error_code
        row.finished_at = _utcnow()
        row.duration_ms = dt_ms
        db.commit()

        if conv_id:
            append_message(db, conv_id, org_id, "tool",
                           {"type": "step_result", "task_id": task_id,
                            "step_idx": s.index, "tool_id": s.tool_id,
                            "status": row.status,
                            "artifact_ref": result.artifact_ref,
                            "warnings": result.warnings,
                            "error_code": result.error_code,
                            "duration_ms": dt_ms})

        if not ok:
            if optional:
                partial = True
                logger.warning("agent_run %s 可选步骤失败 step=%d tool=%s: %s",
                               task_id, s.index, s.tool_id, row.error)
                continue
            failed_idx, failed_error = s.index, row.error
            break
        succeeded_steps += 1
        repo.update_progress(task_id, int((s.index + 1) / n * 100))

    # 收尾
    payload = dict((_load_task_fresh(db, task_id).payload_json) or {})
    if failed_idx is not None:
        payload["run_status"] = RUN_STATUS_FAILED
        payload["summary"] = {"failed_step": failed_idx,
                              "error": failed_error,
                              "dedup_skipped": dedup_skipped}
        repo.update_payload(task_id, payload)
        repo.set_status(task_id, "failed",
                        error=f"步骤 {failed_idx} 失败：{failed_error}")
        if conv_id:
            append_message(db, conv_id, org_id, "assistant",
                           {"type": "run_failed", "task_id": task_id,
                            "failed_step": failed_idx,
                            "error": failed_error})
        return {"run_status": RUN_STATUS_FAILED, "task_id": task_id,
                "failed_step": failed_idx, "error": failed_error}

    final_status = RUN_STATUS_PARTIAL if partial else RUN_STATUS_COMPLETED
    payload["run_status"] = final_status
    payload["summary"] = {"partial": partial,
                          "dedup_skipped": dedup_skipped,
                          "step_count": n}
    repo.update_payload(task_id, payload)
    repo.set_status(task_id, "succeeded", progress=100,
                    result={"run_status": final_status,
                            "summary": payload["summary"]})
    if conv_id:
        append_message(db, conv_id, org_id, "assistant",
                       {"type": "run_done", "task_id": task_id,
                        "run_status": final_status,
                        "summary": payload["summary"]})
    # 取消优先复查：执行期间被取消 → 以 cancelled 落库（绝不误报完成）
    if is_cancelled():
        repo.set_status(task_id, "cancelled", error="任务被取消")
        payload["run_status"] = RUN_STATUS_CANCELLED
        repo.update_payload(task_id, payload)
        return {"run_status": RUN_STATUS_CANCELLED, "task_id": task_id}
    return {"run_status": final_status, "task_id": task_id,
            "summary": payload["summary"]}


# ---------------------------------------------------------------------------
# queue runner 入口（RUNNERS 注册用）
# ---------------------------------------------------------------------------

def run_agent_run(task_id: str, is_cancelled: Callable[[], bool],
                  payload: dict[str, Any]) -> dict[str, Any]:
    """agent_run 的 queue runner：run_agent_run(task_id, is_cancelled, payload)。

    payload 仅用于定位 org（_org_id）；plan 等一律重读 DB（无状态丢失，
    刷新/恢复/confirm 后重入都一致）。
    """
    from ...auth.database import get_session_factory

    db = get_session_factory()()
    try:
        return execute_run_steps(db, task_id, is_cancelled)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 用户确认（waiting_user → 恢复执行）
# ---------------------------------------------------------------------------

def confirm_step(db: Session, task_id: str, step_idx: int,
                 approved: bool,
                 org_id: Optional[str] = None) -> dict[str, Any]:
    """确认等待中的步骤。

    - org_id 非空时校验归属，不匹配抛 EntityNotFound；
    - 非 waiting_user 状态或 step_idx 对不上 → ValueError；
    - approved=False → 任务 cancelled（用户拒绝）；
    - approved=True → **只落确认态**（confirmed_steps / pending_step=None /
      run_status=running），不直接执行剩余步骤；调用方（路由层）负责把任务
      重新入队，由 worker（run_agent_run）异步恢复执行（4a 遗留修复：
      不在 HTTP 请求内同步跑完剩余步骤，避免请求阻塞/超时）。
    """
    repo = AITaskRepository(db)
    task = _load_task_fresh(db, task_id, org_id)
    payload = dict(task.payload_json or {})
    if payload.get("run_status") != RUN_STATUS_WAITING_USER:
        raise ValueError(
            f"任务不在等待确认状态（run_status={payload.get('run_status')}）")
    pending = payload.get("pending_step") or {}
    if pending.get("step_idx") != step_idx:
        raise ValueError(
            f"待确认步骤为 {pending.get('step_idx')}，与请求 {step_idx} 不一致")

    conv_id = payload.get("conversation_id")
    org = task.org_id

    if not approved:
        row = (db.query(AgentStep)
                 .filter(AgentStep.task_id == task_id,
                         AgentStep.status == STEP_WAITING_USER)
                 .order_by(AgentStep.started_at.desc()).first())
        if row is not None:
            row.status = STEP_CANCELLED
            row.finished_at = _utcnow()
            db.commit()
        payload["run_status"] = RUN_STATUS_CANCELLED
        payload["pending_step"] = None
        repo.update_payload(task_id, payload)
        repo.set_status(task_id, "cancelled", error="用户拒绝执行该步骤")
        if conv_id:
            append_message(db, conv_id, org, "assistant",
                           {"type": "run_cancelled", "task_id": task_id,
                            "reason": "用户拒绝确认步骤",
                            "step_idx": step_idx})
        return {"run_status": RUN_STATUS_CANCELLED, "task_id": task_id}

    confirmed = set(payload.get("confirmed_steps") or [])
    confirmed.add(step_idx)
    payload["confirmed_steps"] = sorted(confirmed)
    payload["pending_step"] = None
    payload["run_status"] = RUN_STATUS_RUNNING
    repo.update_payload(task_id, payload)
    if conv_id:
        append_message(db, conv_id, org, "user",
                       {"type": "confirm_step", "task_id": task_id,
                        "step_idx": step_idx, "approved": True})
    logger.info("agent_run %s 步骤 %d 已确认（仅落确认态，等待 worker 恢复）",
                task_id, step_idx)
    return {"run_status": RUN_STATUS_RUNNING, "task_id": task_id,
            "resumed": False,
            "note": "已确认；剩余步骤由 worker 异步恢复执行，"
                    "请轮询 GET /api/v1/agent/runs/{task_id}"}


_PAUSABLE_RUN_STATUSES = (RUN_STATUS_QUEUED, RUN_STATUS_RUNNING,
                          RUN_STATUS_PAUSED)


def pause_run(db: Session, task_id: str,
              org_id: Optional[str] = None) -> dict[str, Any]:
    """用户暂停 run。

    - org_id 非空时校验归属，不匹配抛 EntityNotFound；
    - 终态任务或非 queued/running/paused 的 run_status → ValueError；
    - 只置 payload.paused=True（幂等）；runner 在步骤边界协作式挂起，
      落 run_status=paused（queue 保持 task running，不落终态）。
    """
    repo = AITaskRepository(db)
    task = _load_task_fresh(db, task_id, org_id)
    if task.status in ("succeeded", "failed", "cancelled"):
        raise ValueError(f"任务已终态（{task.status}），无需暂停")
    payload = dict(task.payload_json or {})
    rs = payload.get("run_status")
    if rs not in _PAUSABLE_RUN_STATUSES:
        raise ValueError(f"当前状态 {rs} 不支持暂停")
    payload["paused"] = True
    repo.update_payload(task_id, payload)
    logger.info("agent_run %s 用户请求暂停（run_status=%s）", task_id, rs)
    return {"run_status": rs, "task_id": task_id, "paused": True,
            "note": "已请求暂停，当前步骤完成后挂起；"
                    "请轮询 GET /api/v1/agent/runs/{task_id}"}


def resume_run(db: Session, task_id: str,
               org_id: Optional[str] = None) -> dict[str, Any]:
    """用户继续 run。

    - 未暂停（无 paused 标记且 run_status 不是 paused）→ ValueError；
    - 清 paused 标记；若 runner 已在边界挂起（run_status=paused），
      置回 running 并返回 requeue=True，由调用方（路由层）重新入队恢复执行；
      若 worker 还没走到暂停边界（仍在执行某步骤），只清标记不重入队，
      worker 在下个边界看到 paused=False 会自然继续，避免双 worker 并发。
    """
    repo = AITaskRepository(db)
    task = _load_task_fresh(db, task_id, org_id)
    payload = dict(task.payload_json or {})
    was_paused = bool(payload.get("paused"))
    was_suspended = payload.get("run_status") == RUN_STATUS_PAUSED
    if not was_paused and not was_suspended:
        raise ValueError("任务未处于暂停状态")
    payload["paused"] = False
    if was_suspended:
        payload["run_status"] = RUN_STATUS_RUNNING
    repo.update_payload(task_id, payload)
    logger.info("agent_run %s 用户继续（was_suspended=%s）",
                task_id, was_suspended)
    return {"run_status": payload.get("run_status"), "task_id": task_id,
            "paused": False, "requeue": was_suspended,
            "note": "已继续" + ("；剩余步骤由 worker 异步恢复执行"
                                if was_suspended else "")}
