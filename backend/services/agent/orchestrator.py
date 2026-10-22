"""Phase 5：一句话生成完整项目编排器。

``run_one_shot_brief(db, user, org_id, brief_text, ...)`` 实现任务书 §5.2 的
11 步典型流程：

1. 解析 brief 和作品上下文（parse_brief，模型 + Brief schema + ≤2 次修复）
2. 查询产品档案 / 事实证据（query_products + query_facts，只读，可并行）
3. 建立营销策略与内容目标（generate_strategy，模型）
4. 生成创意方向并推荐（generate_ideas，模型）
5. 生成口播 / 脚本和分镜（generate_storyboard，模型）
6. 生成拍摄执行清单，校验人员 / 设备 / 预算 / 时长（shooting_checklist，确定性）
7. 风险审核和可行性检查（check_risk 确定性规则层必跑）
8. 有限局部修复（≤2 轮）：高风险宣称标"待核实/不可作为确定宣传"，再重跑校验
9. 平台文案 / 交付包（generate_platform_copy 可选 + check_delivery 确定性）
10. 写回作品 + 版本（patch_work 串行写；保存后重读验证持久化）
11. 交付摘要、风险与待核实项，落真实终态

约束落实：
- 每步走 planner 白名单分派（``run_tool``：白名单 + 参数 schema + RBAC +
  结构化错误）；不让模型单次输出整份项目（每步都是聚焦的 JSON 生成）；
- 数据相关的只读步骤可并行（ThreadPoolExecutor + 独立会话），同一作品的
  写操作严格串行（本流程只有一次 patch_work 写）；
- 每步 gateway 调用传 ``task_id=<agent_run 的 AITask.id>``（成本归属到 run）；
- run 级上限沿用 planner：总工具步 ≤25、模型步 ≤8，超限进 partial 保留成果；
- 429 是硬停止线：撞 429 立刻停止该服务商后续调用、上报，不重试不换端点；
- patch_work 的 confirmed=True：用户"一句话生成完整项目"的请求本身就是
  对全流程（含写回作品）的授权；高风险未决项一律标待核实并在摘要中声明，
  不作为确定宣传。
"""
from __future__ import annotations

# 导入顺序约定（仓库既有坑）：backend.models 不能作为首个 backend.* 导入。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ...ai_tasks.repository import AITaskRepository
from ...models import Membership
from ...repositories.base import EntityNotFound
from ...repositories.work_repository import WorkRepository
from .adapters import (
    ModelStepsExceeded,
    RateLimitedStop,
    ToolDeps,
    ToolResult,
    run_tool,
)
from .brief import (
    Brief,
    BriefParseError,
    brief_to_prompt_text,
    parse_brief_from_text,
)
from .context import AgentIdentity
from .conversations import AgentStep, append_message
from .planner import MAX_MODEL_STEPS_PER_RUN, MAX_PLAN_STEPS
from .quality_gates import (
    check_fact_grounding,
    run_quality_gates,
    verify_persistence,
)
from .runner import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_RUNNING,
    create_agent_run,
    step_params_digest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ModelStepsExceeded",
    "RateLimitedStop",
    "run_one_shot_brief",
]


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("429" in msg or "rate_limit" in msg or "ratelimit" in msg
            or "限流" in msg or "too many requests" in msg)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 模型调用封装：计数 / 上限 / 429 硬停 / task_id 成本归属 / 可观测字段
# ---------------------------------------------------------------------------

class _ModelCaller:
    """每步 gateway 调用的统一出口。

    - 每次 generate() = 1 个模型步（JSON 解析层修复计在 attempts 内）；
    - 构造 gateway 时传 task_id=<agent_run 的 AITask.id>（成本归属到 run）；
    - 撞 429 → 置 rate_limited，后续 generate 直接抛 RateLimitedStop，
      不重试、不换端点；
    - fake=(prompt, feedback)->dict 测试注入时不走真实网关。
    """

    def __init__(self, task_id: str, org_id: str,
                 model: Optional[str] = None,
                 fake: Optional[Callable[[str, Optional[str]], dict]] = None):
        self.task_id = task_id
        self.org_id = org_id
        self.model = model
        self.fake = fake
        self.calls = 0
        self.rate_limited = False
        self.last_attempts = 0
        self.last_usage: list[dict] = []
        self.last_model_name: Optional[str] = None
        self.last_duration_ms = 0

    def _new_gateway(self):
        # 函数内导入：避免 services.agent ↔ ai_tasks 顶层循环导入
        from ...ai_tasks.runners import _gateway_for_runner
        return _gateway_for_runner(self.task_id, {"_org_id": self.org_id})

    def generate(self, prompt: str, feedback: Optional[str] = None,
                 task_type: str = "agent_step") -> dict[str, Any]:
        if self.rate_limited:
            raise RateLimitedStop("已触发 429 硬停止，不再发起模型调用")
        if self.calls >= MAX_MODEL_STEPS_PER_RUN:
            raise ModelStepsExceeded(
                f"模型步数 {self.calls} 已达 run 上限 "
                f"{MAX_MODEL_STEPS_PER_RUN}")
        t0 = time.time()
        try:
            if self.fake is not None:
                obj = self.fake(prompt, feedback)
                self.last_attempts, self.last_usage = 1, []
                self.last_model_name = "fake"
            else:
                gw, gdb = self._new_gateway()
                try:
                    def _make_messages(fb: Optional[str]):
                        p = prompt if not fb else f"{prompt}\n\n修正要求：{fb}"
                        return [{"role": "user", "content": p}]

                    obj, meta = gw.generate_with_retry(
                        task_type, _make_messages,
                        lambda d: ([], d),
                        model=self.model, max_attempts=3)
                    self.last_attempts = int(meta.get("attempts") or 0)
                    self.last_usage = list(meta.get("usage") or [])
                    # 记录实际生效模型名（显式指定优先，否则服务端默认）
                    from ...ai_service import DEFAULT_MODEL
                    self.last_model_name = self.model or DEFAULT_MODEL
                finally:
                    gdb.close()
            if not isinstance(obj, dict):
                raise ValueError("模型调用返回非 JSON 对象")
            return obj
        except (RateLimitedStop, ModelStepsExceeded):
            raise
        except Exception as e:  # noqa: BLE001
            if _is_rate_limit_error(e):
                self.rate_limited = True
                raise RateLimitedStop(
                    f"撞 429 限流，硬停止：{e}") from e
            raise
        finally:
            self.calls += 1
            self.last_duration_ms = int((time.time() - t0) * 1000)


# ---------------------------------------------------------------------------
# 编排上下文
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, db: Session, identity: AgentIdentity,
                 task_id: str, caller: _ModelCaller,
                 session_factory: Callable[[], Session],
                 is_cancelled: Optional[Callable[[], bool]] = None):
        self.db = db
        self.identity = identity
        self.task_id = task_id
        self.caller = caller
        self.session_factory = session_factory
        self.is_cancelled = is_cancelled or (lambda: False)
        self.deps = ToolDeps(
            db=db, identity=identity,
            ai_generate=lambda prompt: caller.generate(prompt),
            model=caller.model)
        self.tool_steps = 0          # run 级工具步计数（上限 25）
        self.step_log: list[dict] = []


def _check_cancel(ctx: _Ctx) -> None:
    if ctx.is_cancelled():
        from ...ai_tasks.queue import _TaskCancelled
        raise _TaskCancelled()


def _record_step(ctx: _Ctx, tool_id: str, params: dict[str, Any],
                 result: ToolResult, duration_ms: int) -> None:
    row = AgentStep(
        task_id=ctx.task_id, tool_id=tool_id,
        params_digest=step_params_digest(params),
        status="succeeded" if result.status == "ok" else "failed",
        output_ref=result.artifact_ref,
        error=result.error_message or result.error_code,
        started_at=_utcnow(), finished_at=_utcnow(),
        duration_ms=duration_ms)
    ctx.db.add(row)
    ctx.db.commit()
    ctx.step_log.append({"tool_id": tool_id, "status": row.status,
                         "duration_ms": duration_ms,
                         "error": row.error})


def _run_tool_step(ctx: _Ctx, tool_id: str, params: dict[str, Any],
                   *, optional: bool = False,
                   confirmed: bool = False) -> ToolResult:
    """走白名单分派执行一步并记 agent_steps。超 25 步直接抛（转 partial）。"""
    _check_cancel(ctx)
    if ctx.tool_steps >= MAX_PLAN_STEPS:
        raise ModelStepsExceeded(
            f"工具步数 {ctx.tool_steps} 已达 run 上限 {MAX_PLAN_STEPS}")
    ctx.tool_steps += 1
    t0 = time.time()
    try:
        result = run_tool(ctx.deps, tool_id, params, confirmed=confirmed)
    except (ModelStepsExceeded, RateLimitedStop):
        # 控制流异常穿透：run 级上限 / 429 硬停由主流程统一转 partial
        raise
    except Exception as e:  # noqa: BLE001  兜底：结构化
        result = ToolResult(status="failed", error_code="internal",
                            error_message=f"{type(e).__name__}: {e}")
    _record_step(ctx, tool_id, params, result,
                 int((time.time() - t0) * 1000))
    if result.status != "ok":
        logger.warning("one_shot %s 步骤 %s %s: %s",
                       ctx.task_id, tool_id,
                       "可选失败" if optional else "失败",
                       result.error_message or result.error_code)
    return result


def _parallel_read_steps(ctx: _Ctx,
                         calls: list[tuple[str, dict[str, Any]]]
                         ) -> list[ToolResult]:
    """只读步骤并行（独立会话 + 线程池）；写操作绝不走这里。"""
    results: list[Optional[ToolResult]] = [None] * len(calls)

    def _one(i: int, tool_id: str, params: dict[str, Any]) -> None:
        s = ctx.session_factory()
        try:
            deps = ToolDeps(db=s, identity=ctx.identity,
                            ai_generate=None, model=ctx.caller.model)
            results[i] = run_tool(deps, tool_id, params)
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=max(1, len(calls))) as ex:
        futs = [ex.submit(_one, i, t, p) for i, (t, p) in enumerate(calls)]
        for f in futs:
            f.result()
    out: list[ToolResult] = []
    for i, (tool_id, params) in enumerate(calls):
        r = results[i] or ToolResult(status="failed", error_code="internal",
                                     error_message="并行步骤无返回")
        ctx.tool_steps += 1
        _record_step(ctx, tool_id, params, r, 0)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_one_shot_brief(
    db: Session,
    user: Any,
    org_id: str,
    brief_text: str,
    *,
    work_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    model: Optional[str] = None,
    ai_generate: Optional[Callable[[str, Optional[str]], dict]] = None,
    session_factory: Optional[Callable[[], Session]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """一句话生成完整项目（任务书 §5.2 11 步）。同步执行；调用方决定线程/队列。

    task_id 可选：传入已创建的 run 则复用（HTTP /one-shot 路由先建 run
    再入队时用）；不传则内部新建。

    返回 {"task_id","run_status","work_id","conversation_id","summary",
    "quality",...}。业务库只读 + 经 adapter 的受控写；测试一律用 scratch 库。
    """
    from ...auth.database import get_session_factory as _gsf
    # _Ctx 要的是 Callable[[], Session]；get_session_factory 本体返回 sessionmaker，
    # 需再包一层才能拿到 Session
    sf = session_factory or (lambda: _gsf()())

    user_id = user if isinstance(user, str) else (
        getattr(user, "user_id", None) or getattr(user, "id", None))
    if not user_id:
        raise ValueError("无法从 user 解析 user_id")
    ms = db.query(Membership).filter_by(
        user_id=str(user_id), org_id=org_id).first()
    role = ms.role if ms else None
    if role is None:
        raise EntityNotFound("Organization", org_id)
    identity = AgentIdentity(user_id=str(user_id), org_id=org_id, role=role)

    # 建 run（pending → running）；brief 先存原文，解析后回填结构化版本
    if task_id is None:
        task_id = create_agent_run(
            db, user, org_id, work_id=work_id,
            brief_json={"text": (brief_text or "")[:2000]},
            conversation_id=conversation_id)
    repo = AITaskRepository(db)
    # 队列 _execute 已置 running（/one-shot 入队路径）；直接调用时才需 pending→running
    _cur = repo.get(task_id)
    if _cur is not None and _cur.status == "pending":
        repo.set_status(task_id, "running")
    conv_id = (repo.get(task_id).payload_json or {}).get("conversation_id")

    caller = _ModelCaller(task_id, org_id, model=model, fake=ai_generate)
    ctx = _Ctx(db, identity, task_id, caller, sf, is_cancelled)

    summary: dict[str, Any] = {
        "brief_text": (brief_text or "")[:2000],
        "steps": [], "warnings": [], "errors": [],
        "model_calls": 0, "items_to_verify": [],
        "confirmed_defaults": [],
    }
    artifacts: dict[str, Any] = {}
    facts: list[dict[str, Any]] = []
    final_work_id = work_id
    work_version: Optional[int] = None

    def _finish(run_status: str, task_status: str,
                error: Optional[str] = None) -> dict[str, Any]:
        summary["steps"] = ctx.step_log
        summary["model_calls"] = caller.calls
        payload = dict((repo.get(task_id).payload_json) or {})
        payload["run_status"] = run_status
        payload["summary"] = summary
        repo.update_payload(task_id, payload)
        if task_status == "succeeded":
            repo.set_status(task_id, "succeeded", progress=100,
                            result={"run_status": run_status,
                                    "summary": summary})
        else:
            repo.set_status(task_id, task_status,
                            error=error or summary.get("error"))
        if conv_id:
            try:
                append_message(db, conv_id, org_id, "assistant",
                               {"type": "one_shot_summary",
                                "task_id": task_id,
                                "run_status": run_status,
                                "work_id": final_work_id,
                                "summary": _summary_for_chat(summary)})
            except Exception:  # noqa: BLE001  摘要落盘失败不影响终态
                logger.warning("one_shot %s 摘要消息落盘失败", task_id,
                               exc_info=True)
        return {
            "task_id": task_id,
            "run_status": run_status,
            "work_id": final_work_id,
            "work_version": work_version,
            "conversation_id": conv_id,
            "summary": summary,
            "quality": summary.get("quality"),
        }

    def _partial(reason: str) -> dict[str, Any]:
        summary["warnings"].append(reason)
        return _finish(RUN_STATUS_PARTIAL, "succeeded")

    def _failed(reason: str) -> dict[str, Any]:
        summary["error"] = reason
        summary["errors"].append(reason)
        return _finish(RUN_STATUS_FAILED, "failed", error=reason)

    try:
        # ---- 步骤 1：解析 brief（模型 + Brief schema + ≤2 次受控修复）----
        r = _run_tool_step(ctx, "parse_brief", {"user_text": brief_text})
        if r.status != "ok":
            return _failed(f"brief 解析失败：{r.error_message}")
        brief = Brief.model_validate(r.data["brief"])
        summary["brief"] = brief.model_dump()
        summary["confirmed_defaults"] = brief.confirmed_defaults
        summary["items_to_verify"] = brief.items_to_verify
        brief_text_for_tools = brief_to_prompt_text(brief)

        # ---- 步骤 2：查产品档案 / 事实证据（只读，可并行）----
        prod_params: dict[str, Any] = {}
        if brief.product_id:
            prod_params["product_id"] = brief.product_id
        elif brief.product_name:
            prod_params["keyword"] = brief.product_name
        fact_params: dict[str, Any] = {"status": "usable", "limit": 50}
        if brief.product_id:
            fact_params["product_id"] = brief.product_id
        q_results = _parallel_read_steps(
            ctx, [("query_products", prod_params),
                  ("query_facts", fact_params)])
        r_products, r_facts = q_results
        product: Optional[dict[str, Any]] = None
        if r_products.status == "ok" and r_products.data.get("products"):
            product = r_products.data["products"][0]
            if not brief.product_id:
                brief.product_id = product["id"]
        if r_products.status != "ok":
            summary["warnings"].append(
                f"产品档案查询失败（继续用用户输入推进）：{r_products.error_message}")
        if r_facts.status == "ok":
            facts = r_facts.data.get("facts") or []
        else:
            summary["warnings"].append(
                f"事实库查询失败（风险门禁将更严格）：{r_facts.error_message}")
        summary["product"] = product
        summary["fact_count"] = len(facts)

        # ---- 步骤 3：营销策略与内容目标 ----
        r = _run_tool_step(ctx, "generate_strategy", {
            "brief": brief_text_for_tools,
            "product_id": brief.product_id,
        })
        if r.status != "ok":
            return _failed(f"策略生成失败：{r.error_message}")
        artifacts["strategy"] = r.data["strategy"]

        # ---- 步骤 4：创意方向并推荐 ----
        r = _run_tool_step(ctx, "generate_ideas", {
            "brief": brief_text_for_tools,
            "product_id": brief.product_id,
            "idea_count": 3,
        })
        if r.status != "ok":
            return _failed(f"创意生成失败：{r.error_message}")
        creative = {"directions": r.data["ideas"], "recommended_index": 0}
        artifacts["creative"] = creative
        chosen = creative["directions"][0]
        chosen_text = (f"{chosen.get('title','')}：{chosen.get('angle','')}；"
                       f"卖点：{'、'.join(chosen.get('selling_points') or [])}")

        # ---- 步骤 5：口播 / 脚本和分镜 ----
        duration = brief.video_duration_sec or 60
        shot_count = max(3, min(10, round(duration / 12)))
        r = _run_tool_step(ctx, "generate_storyboard", {
            "brief": brief_text_for_tools,
            "idea": chosen_text,
            "shot_count": shot_count,
            "duration_sec": duration,
        })
        if r.status != "ok":
            return _failed(f"分镜生成失败：{r.error_message}")
        artifacts["storyboard"] = r.data["storyboard"]

        # ---- 步骤 6：拍摄执行清单 + 人员/设备/预算/时长校验 ----
        r = _run_tool_step(ctx, "shooting_checklist", {
            "storyboard": artifacts["storyboard"],
        })
        if r.status != "ok":
            return _failed(f"拍摄清单生成失败：{r.error_message}")
        shooting = dict(r.data)
        shooting_list = {
            "items": [
                {"shot_no": it.get("shot_no"),
                 "scene": it.get("scene", ""),
                 "duration_sec": it.get("duration_sec") or 0,
                 "people_needed": 1,
                 "devices_needed": brief.devices,
                 "props": []}
                for it in r.data.get("checklist", [])
            ],
            "total_duration_sec": sum(
                (it.get("duration_sec") or 0)
                for it in r.data.get("checklist", [])),
            "crew_size": brief.people_count or 1,
            "devices_available": brief.devices,
        }
        artifacts["shooting_list"] = shooting_list
        summary["shooting"] = {
            "todo_count": r.data.get("todo_count", 0),
            "reshoot_count": r.data.get("reshoot_count", 0)}
        # 约束校验（人员/设备/预算/时长），问题记入 warnings（门禁再判）
        _check_shoot_constraints(brief, artifacts["shooting_list"],
                                 summary["warnings"])

        # ---- 步骤 7：风险审核（确定性规则层必跑）----
        selling_points = []
        for d in creative["directions"]:
            selling_points.extend(d.get("selling_points") or [])
        r = _run_tool_step(ctx, "check_risk", {
            "storyboard": artifacts["storyboard"],
            "selling_points": selling_points[:20],
        })
        if r.status != "ok":
            return _failed(f"风险审核失败：{r.error_message}")
        risk_report = r.data
        artifacts["risk_report"] = risk_report

        # ---- 步骤 8：有限局部修复（≤2 轮）----
        repaired = _limited_repair(ctx, brief, artifacts, facts,
                                   summary["warnings"])
        summary["repaired_claims"] = repaired
        # 重跑事实引用校验
        ground_issues = check_fact_grounding(brief, artifacts, facts)
        if ground_issues:
            # 仍有未解决的高风险 → 标待核实（确定性兜底，不静默通过）
            _mark_unverified(artifacts, ground_issues)
            summary["warnings"].append(
                "仍有高风险宣称无事实支撑，已标“待核实/不可作为确定宣传”："
                + "; ".join(i.message for i in ground_issues[:3]))
            ground_issues = check_fact_grounding(brief, artifacts, facts)
        summary["risk"] = {
            "flags": risk_report.get("flags", []),
            "high_risk_count": risk_report.get("high_risk_count", 0),
            "passed": risk_report.get("passed", False),
            "grounding_issues": [i.model_dump() for i in ground_issues],
        }

        # ---- 步骤 9：平台文案 / 交付包（文案可选，不阻塞核心）----
        voiceover_all = "\n".join(
            s.get("voiceover", "")
            for s in artifacts["storyboard"].get("shots", []))
        copy_failed = False
        try:
            r = _run_tool_step(ctx, "generate_platform_copy", {
                "brief": brief_text_for_tools,
                "platform": brief.platform,
                "creative": chosen_text,
                "voiceover": voiceover_all[:2000],
                "product_name": brief.product_name,
            }, optional=True)
            if r.status == "ok":
                artifacts["copy"] = r.data["copy"]
            else:
                copy_failed = True
                summary["warnings"].append(
                    f"平台文案生成失败（可选步骤，不阻塞）：{r.error_message}")
        except (ModelStepsExceeded, RateLimitedStop):
            copy_failed = True
            summary["warnings"].append("平台文案因模型步数/限流跳过（可选步骤）")
        r = _run_tool_step(ctx, "check_delivery", {
            "storyboard": artifacts["storyboard"],
            "check_type": "plan",
        })
        delivery = r.data.get("delivery") if r.status == "ok" else None
        if r.status != "ok":
            summary["warnings"].append(
                f"交付检查失败：{r.error_message}")
        artifacts["delivery"] = delivery or {}

        # ---- 步骤 10：写回作品 + 版本（串行写；保存后重读验证）----
        if final_work_id:
            w = WorkRepository(db).get(final_work_id, org_id)
            if w is None:
                return _failed("关联作品不存在或不属于该组织")
        else:
            title = (f"{brief.product_name}·AI 创作"
                     if brief.product_name else "AI 一句话创作")
            w = WorkRepository(db).create(
                org_id=org_id, owner_id=str(user_id), title=title,
                data={}, status="draft")
            db.commit()
            final_work_id = w.id
            summary["work_created"] = True
        # 写前重读拿最新版本号（乐观锁）
        w = WorkRepository(db).get(final_work_id, org_id)
        if w is None:
            return _failed("写回前重读作品失败")
        data_patch = {
            "brief": brief.model_dump(),
            "strategy": artifacts["strategy"],
            "creative": artifacts["creative"],
            "storyboard": artifacts["storyboard"],
            "shooting_list": artifacts["shooting_list"],
            "risk_report": artifacts["risk_report"],
        }
        if "copy" in artifacts:
            data_patch["copy"] = artifacts["copy"]
        # 用户"一句话生成完整项目"即对全流程（含写回）的授权
        r = _run_tool_step(ctx, "patch_work", {
            "work_id": final_work_id,
            "expected_version": w.version,
            "data_patch": data_patch,
        }, confirmed=True)
        if r.status != "ok":
            return _failed(f"写回作品失败：{r.error_message}")
        work_version = r.data.get("version")
        summary["work"] = {"work_id": final_work_id,
                           "version": work_version}

        # ---- 步骤 11：质量门禁 → 交付摘要 → 真实终态 ----
        required_kinds = ["strategy", "creative", "storyboard",
                          "shooting_list"]
        if "copy" in artifacts:
            required_kinds.append("copy")
        quality = run_quality_gates(
            brief, artifacts, facts, db=db,
            work_id=final_work_id, org_id=org_id,
            required_kinds=required_kinds)
        summary["quality"] = quality.model_dump()
        summary["deliverables"] = brief.deliverables

        if not quality.passed:
            summary["warnings"].append("质量门禁未通过：" + quality.summary)
            return _finish(RUN_STATUS_PARTIAL, "succeeded")
        if copy_failed or caller.rate_limited:
            return _partial("部分可选步骤未完成（文案/限流），核心项目已生成")
        return _finish(RUN_STATUS_COMPLETED, "succeeded")

    except RateLimitedStop as e:
        summary["warnings"].append(str(e))
        summary["errors"].append("rate_limited")
        return _finish(RUN_STATUS_PARTIAL, "succeeded")
    except ModelStepsExceeded as e:
        summary["warnings"].append(str(e))
        return _finish(RUN_STATUS_PARTIAL, "succeeded")
    except BriefParseError as e:
        return _failed(str(e))
    except Exception as e:  # noqa: BLE001
        from ...ai_tasks.queue import _TaskCancelled
        if isinstance(e, _TaskCancelled):
            payload = dict((repo.get(task_id).payload_json) or {})
            payload["run_status"] = "cancelled"
            repo.update_payload(task_id, payload)
            repo.set_status(task_id, "cancelled", error="任务被取消")
            return {"task_id": task_id, "run_status": "cancelled",
                    "work_id": final_work_id, "conversation_id": conv_id,
                    "summary": summary}
        logger.exception("one_shot %s 未预期异常", task_id)
        return _failed(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------

def _check_shoot_constraints(brief: Brief, shooting_list: dict,
                             warnings: list[str]) -> None:
    """步骤 6 的人员/设备/预算/时长校验；问题记入 warnings（门禁再判）。"""
    items = shooting_list.get("items") or []
    if brief.people_count:
        need = max([i.get("people_needed", 1) for i in items] or [1])
        if shooting_list.get("crew_size", 1) > brief.people_count:
            warnings.append(
                f"拍摄需要 {shooting_list['crew_size']} 人，"
                f"超过 brief 约束的 {brief.people_count} 人")
        if need > brief.people_count:
            warnings.append(f"有镜头需要 {need} 人同时出镜，超过约束")
    if brief.devices:
        missing = sorted({d for i in items for d in i.get("devices_needed", [])}
                         - set(brief.devices))
        if missing:
            warnings.append(f"拍摄需要但 brief 未列出的设备：{'、'.join(missing)}")
    if brief.budget_yuan is not None and brief.budget_yuan < 500:
        warnings.append(f"预算仅 {brief.budget_yuan} 元：建议单人单机、自然光、"
                        "零置景拍摄，清单已按最小配置生成")
    target = brief.video_duration_sec or 0
    total = shooting_list.get("total_duration_sec") or 0
    if target > 0 and total > 0 and abs(total - target) / target > 0.3:
        warnings.append(f"拍摄清单总时长 {total:.0f}s 与目标 {target}s 偏差超 30%")


def _limited_repair(ctx: _Ctx, brief: Brief, artifacts: dict[str, Any],
                    facts: list[dict[str, Any]],
                    warnings: list[str]) -> list[dict[str, Any]]:
    """步骤 8：有限局部修复（≤2 轮）。

    高风险宣称无事实引用 → 在原句后追加"（待核实，不可作为确定宣传）"
    的确定性修复；每轮后重跑事实引用校验。返回修复记录。
    """
    repaired: list[dict[str, Any]] = []
    for _round in range(2):
        issues = check_fact_grounding(brief, artifacts, facts)
        if not issues:
            break
        for issue in issues:
            sent = (issue.detail or {}).get("sentence", "")
            source = (issue.detail or {}).get("source", "")
            if not sent:
                continue
            if _append_unverified_marker(artifacts, source, sent):
                repaired.append({"round": _round + 1, "source": source,
                                 "sentence": sent[:60]})
        if not repaired:
            break
    if repaired:
        warnings.append(f"已对 {len(repaired)} 处高风险宣称标待核实")
    return repaired


def _append_unverified_marker(artifacts: dict[str, Any], source: str,
                              sentence: str) -> bool:
    """把待核实标记追加到对应 artifact 的原句上。成功返回 True。"""
    marker = "（待核实，不可作为确定宣传）"
    if marker in sentence:
        return False
    new_sent = sentence + marker
    if source.startswith("镜头"):
        try:
            no = int("".join(ch for ch in source.split("镜头")[1].split("口播")[0]
                             if ch.isdigit()))
        except (IndexError, ValueError):
            return False
        for s in (artifacts.get("storyboard") or {}).get("shots", []):
            if s.get("shot_no") == no:
                for key in ("voiceover", "subtitle"):
                    if sentence in (s.get(key) or ""):
                        s[key] = s[key].replace(sentence, new_sent)
                        return True
        return False
    if source.startswith("创意方向"):
        for d in (artifacts.get("creative") or {}).get("directions", []):
            sps = d.get("selling_points") or []
            for i, sp in enumerate(sps):
                if sentence in str(sp):
                    sps[i] = str(sp).replace(sentence, new_sent)
                    return True
        return False
    if source == "平台文案":
        copy = artifacts.get("copy") or {}
        if sentence in (copy.get("text") or ""):
            copy["text"] = copy["text"].replace(sentence, new_sent)
            return True
    return False


def _mark_unverified(artifacts: dict[str, Any],
                     issues: list) -> None:
    """兜底：仍未解决的高风险句全部标待核实（不静默通过）。"""
    for issue in issues:
        _append_unverified_marker(artifacts,
                                 (issue.detail or {}).get("source", ""),
                                 (issue.detail or {}).get("sentence", ""))


def _summary_for_chat(summary: dict[str, Any]) -> dict[str, Any]:
    """写进 conversation 的摘要精简版（不含超长原文）。"""
    brief = summary.get("brief") or {}
    return {
        "product": brief.get("product_name"),
        "duration_sec": brief.get("video_duration_sec"),
        "items_to_verify": summary.get("items_to_verify", []),
        "confirmed_defaults": summary.get("confirmed_defaults", []),
        "warnings": summary.get("warnings", [])[:10],
        "quality": (summary.get("quality") or {}).get("summary"),
        "model_calls": summary.get("model_calls", 0),
    }
