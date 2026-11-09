"""Phase 2：Agent 工具适配器。

每个工具的适配器只调用既有 service/repository（见 TOOL_MAPPING.md），
禁止直写 DB、禁止绕过业务规则。统一结果信封：
    status ∈ {ok, partial, failed}
    data / artifact_ref / warnings / error_code

权限模型（PHASE1_DESIGN.md §5）：
- 身份只从服务端 AgentIdentity 取；Membership 表重读确认组织归属与角色。
- 对象（work/storyboard/task）按 org_id 重读；跨组织或不存在一律返回
  not_found（404 防枚举，不向无权用户确认资源存在）。
- 角色无对应 action 权限 → forbidden。
error_code 枚举：invalid_params / unknown_tool / forbidden / not_found /
    needs_confirmation / needs_model / locked / conflict / validation /
    model_error / internal
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models
from ...rbac import has_permission
from ...repositories.base import EntityNotFound, OptimisticLockError
from ...repositories.fact_repository import FactRepository
from ...repositories.product_repository import ProductRepository
from ...repositories.storyboard_repository import StoryboardRepository
from ...repositories.work_repository import WorkRepository
from ...risk import check_fact_grounding
from ...services import ai_ledger, delivery_service
from .. import risk_service
from .context import AgentIdentity
from ...validator import _normalize_storyboard_for_validation  # 共享入口，定义在 validator.py
from .tools import (
    PATCHABLE_DATA_KEYS,
    TOOLS_REGISTRY,
    ToolDefinition,
    ToolParamError,
    ToolTier,
    get_tool,
    is_tool_allowed,
    validate_tool_params,
)

RESULT_OK = "ok"
RESULT_PARTIAL = "partial"
RESULT_FAILED = "failed"


class ModelStepsExceeded(RuntimeError):
    """控制流异常：run 级模型步数超限。

    必须穿透 adapter/run_tool 的结构化错误包装，直达编排层做 partial
    处理；run_tool 见到此类异常直接 re-raise，不转 ToolResult。
    """


class RateLimitedStop(RuntimeError):
    """控制流异常：撞 429 硬停止。

    必须穿透 adapter/run_tool 的结构化错误包装，直达编排层：立刻停止
    该服务商后续调用、上报，不重试不换端点。
    """


_CONTROL_FLOW_EXCEPTIONS = (ModelStepsExceeded, RateLimitedStop)


# ---------------------------------------------------------------------------
# 结果信封 / 异常
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    status: str  # ok | partial | failed
    data: dict[str, Any] = field(default_factory=dict)
    artifact_ref: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": self.data,
            "artifact_ref": self.artifact_ref,
            "warnings": self.warnings,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _ok(data: Optional[dict] = None, artifact_ref: Optional[str] = None,
        warnings: Optional[list[str]] = None) -> ToolResult:
    return ToolResult(status=RESULT_OK, data=data or {},
                      artifact_ref=artifact_ref, warnings=warnings or [])


def _fail(error_code: str, message: str,
          data: Optional[dict] = None) -> ToolResult:
    return ToolResult(status=RESULT_FAILED, data=data or {},
                      error_code=error_code, error_message=message)


class _Forbidden(Exception):
    pass


class _NotFound(Exception):
    pass


class _Conflict(Exception):
    pass


# ---------------------------------------------------------------------------
# 依赖注入容器
# ---------------------------------------------------------------------------

@dataclass
class ToolDeps:
    """工具执行依赖。

    db/identity 必填（服务端构造）。ai_generate：prompt→解析后 JSON 的可调用对象，
    由 Phase 3 的编排层注入（走 AIGateway.generate_with_retry）；测试时注入假实现。
    ai_service：gateway-like 对象，仅供 check_risk 的 AI 识别层使用。
    """
    db: Session
    identity: AgentIdentity
    ai_generate: Optional[Callable[[str], dict]] = None
    ai_service: Any = None
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# 权限 / 归属校验
# ---------------------------------------------------------------------------

def _resolve_role(db: Session, identity: AgentIdentity) -> str:
    """从 Membership 表重读角色；非本组织成员 → _Forbidden。"""
    ms = db.query(models.Membership).filter_by(
        user_id=identity.user_id, org_id=identity.org_id).first()
    if ms is None:
        raise _Forbidden(f"用户不是组织 {identity.org_id} 的成员")
    return ms.role


def _require_action(db: Session, identity: AgentIdentity, action: str) -> str:
    role = _resolve_role(db, identity)
    if not has_permission(role, action):
        raise _Forbidden(f"角色 {role} 缺少权限 {action}")
    return role


def _get_work_owned(db: Session, org_id: str, work_id: str) -> models.Work:
    """按 org 重读作品；不存在/跨组织/已删除 → _NotFound（防枚举）。"""
    w = WorkRepository(db).get(work_id, org_id)
    if w is None or w.deleted_at is not None:
        raise _NotFound("作品不存在")
    return w


def _require_ai(deps: ToolDeps, tool_id: str) -> Callable[[str], dict]:
    if deps.ai_generate is None:
        raise _ToolNeedsModel(tool_id)
    return deps.ai_generate


class _ToolNeedsModel(Exception):
    def __init__(self, tool_id: str):
        self.tool_id = tool_id
        super().__init__(f"工具 {tool_id} 需要模型调用，但未注入 ai_generate")


# ---------------------------------------------------------------------------
# 工具适配器（每个只调既有 service/repository）
# ---------------------------------------------------------------------------

def _adapt_query_facts(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "fact:read")
    repo = FactRepository(deps.db)
    if p.fact_id:
        f = repo.get(p.fact_id, deps.identity.org_id)
        if f is None:
            raise _NotFound("事实不存在")
        items = [_fact_dict(f)]
    elif p.status == "usable":
        items = [_fact_dict(f) for f in
                 repo.list_usable(deps.identity.org_id,
                                  product_id=p.product_id, limit=p.limit)]
    else:
        items = [_fact_dict(f) for f in
                 repo.list_by_org(deps.identity.org_id,
                                  status=None if p.status == "all" else p.status,
                                  limit=p.limit)]
        if p.product_id:
            items = [i for i in items if i["product_id"] == p.product_id]
    return _ok({"facts": items, "count": len(items)})


def _fact_dict(f: models.Fact) -> dict[str, Any]:
    return {
        "id": f.id, "content": f.content, "category": f.category,
        "status": f.status, "product_id": f.product_id,
        "source_type": f.source_type,
        "allow_use": f.allow_use,
        "expires_at": f.expires_at.isoformat() if f.expires_at else None,
    }


def _adapt_query_products(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "product:read")
    repo = ProductRepository(deps.db)
    org_id = deps.identity.org_id
    if p.product_id:
        prod = repo.get(p.product_id, org_id)
        if prod is None:
            raise _NotFound("产品不存在")
        items = [prod]
    elif p.code:
        prod = repo.get_by_code(org_id, p.code)
        items = [prod] if prod else []
    else:
        items = list(repo.list_by_org(org_id, limit=p.limit))
        if p.keyword:
            items = [x for x in items
                     if p.keyword in (getattr(x, "name", "") or "")]
            items = items[:p.limit]
    return _ok({"products": [
        {"id": x.id, "code": getattr(x, "code", None),
         "name": getattr(x, "name", "")} for x in items],
        "count": len(items)})


def _adapt_generate_ideas(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    ai = _require_ai(deps, "generate_ideas")
    facts_text = ""
    if p.inject_confirmed_facts:
        facts = FactRepository(deps.db).list_usable(
            deps.identity.org_id, product_id=p.product_id, limit=20)
        facts_text = "\n".join(f"- {f.content}" for f in facts)
    prompt = (
        "你是农产品短视频营销策划师。根据以下 brief 生成 "
        f"{p.idea_count} 个创意方向，返回 JSON："
        "{\"ideas\": [{\"title\": str, \"angle\": str, \"selling_points\": [str]}]}。"
        f"\nbrief：{p.brief}"
        + (f"\n已确认事实（只能引用这些，禁止编造）：\n{facts_text}" if facts_text else "")
    )
    try:
        result = ai(prompt)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except Exception as e:
        return _fail("model_error", f"创意生成模型调用失败: {e}")
    ideas = result.get("ideas") if isinstance(result, dict) else None
    if not isinstance(ideas, list) or not ideas:
        return _fail("model_error", "模型返回结构非法：缺少 ideas 数组")
    return _ok({"ideas": ideas[:p.idea_count]},
               warnings=["结果未落盘：如需保存，请用 patch_work 写回作品"])


def _adapt_generate_storyboard(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    ai = _require_ai(deps, "generate_storyboard")
    from ...validator import validate_dict
    prompt = (
        "你是农产品短视频分镜师。生成分镜 JSON："
        "{\"shots\": [{\"shot_no\": int, \"scene\": str, \"shot_type\": str, "
        "\"duration_sec\": number, \"voiceover\": str, \"subtitle\": str, "
        "\"shoot_notes\": str}]}。"
        f"\n目标时长约 {p.duration_sec} 秒，共 {p.shot_count} 个镜头。"
        f"\nbrief：{p.brief}"
        + (f"\n创意方向：{p.idea}" if p.idea else "")
        + "\n格式一致性要求（否则整份分镜会在校验时失败）：只输出上面"
          "的简化格式即可；如果你要输出 shoot_steps / acceptance_criteria "
          "这类 v2 字段，必须给每个镜头都补齐 shoot_steps（至少3步）、"
          "acceptance_criteria（至少2条）、failure_modes（至少1项，"
          "{\"symptom\": str, \"fix\": str}）、reshoot_guide，缺一不可。"
          "不要混格式：不要有的镜头有 v2 字段、有的镜头没有。"
    )
    # 第6步即校验+重试：此前这里不校验，缺字段的分镜要到第7步拍摄清单
    # 才以“分镜未通过校验”失败。简化格式本身能通过校验（不触发 v2 规则），
    # 所以正常路径不增加模型调用；只有触发 v2 规则又缺字段时才重试，
    # 仍失败则在本步明确失败，不再进入拍摄清单。
    last_errors: list[str] = []
    for attempt in range(3):
        use_prompt = prompt
        if attempt:
            use_prompt = (
                prompt
                + "\n修正要求：上次输出未通过校验，请修正后重新输出完整 JSON"
                  "（只输出 JSON，不要加解释文字），不要丢镜头：\n"
                + "\n".join(f"- {e}" for e in last_errors)
                + "\n上次输出（请在此基础上补齐缺失字段）：\n"
                + json.dumps({"shots": last_shots}, ensure_ascii=False)
            )
        try:
            result = ai(use_prompt)
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except Exception as e:
            return _fail("model_error", f"分镜生成模型调用失败: {e}")
        shots = result.get("shots") if isinstance(result, dict) else None
        if not isinstance(shots, list) or not shots:
            return _fail("model_error", "模型返回结构非法：缺少 shots 数组")
        last_shots = shots
        errors, _ = validate_dict(
            _normalize_storyboard_for_validation({"shots": shots}))
        if not errors:
            return _ok({"storyboard": {"shots": shots}},
                       warnings=["结果未落盘：如需保存，请用 patch_work 写回作品"])
        last_errors = errors
    suffix = f"（等共 {len(last_errors)} 条）" if len(last_errors) > 5 else ""
    return _fail("validation",
                 "分镜生成未通过校验（第6步即失败，不再进入拍摄清单）: "
                 + "; ".join(last_errors[:5]) + suffix,
                 data={"errors": last_errors})


def _adapt_check_risk(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    warnings: list[str] = []
    # 1. 确定性事实 grounding 规则层（必跑，零成本）
    facts = FactRepository(deps.db).list_usable(deps.identity.org_id, limit=200)
    fact_dicts = [{"id": f.id, "content": f.content, "status": f.status,
                   "allow_use": f.allow_use} for f in facts]
    grounding_flags = check_fact_grounding(p.storyboard, fact_dicts)
    flags = [dict(g) for g in grounding_flags]
    # 2. AI 识别层（可选）
    if p.run_ai_layer:
        if deps.ai_service is None or not deps.model:
            warnings.append("ai_layer_skipped：未注入 ai_service/model，只跑规则层")
        else:
            try:
                ai_result = risk_service.check_storyboard_risk(
                    deps.ai_service, p.storyboard,
                    evidence_materials=p.evidence_materials,
                    selling_points=list(p.selling_points),
                    model=deps.model,
                )
                flags.extend(ai_result.get("flags", []))
            except Exception as e:
                # AI 层异常不得被识别为"已通过"（risk_service 约定：上抛）
                return _fail("model_error",
                             f"AI 风险识别层失败（规则层结果仍有效）: {e}",
                             data={"flags": flags})
    high = [f for f in flags if f.get("level") == "高"]
    return _ok({"flags": flags, "flag_count": len(flags),
                "high_risk_count": len(high),
                "passed": len(high) == 0},
               warnings=warnings)


# ---------------------------------------------------------------------------
# 分镜格式归一化（Phase 5 修复：工具链内部格式 vs validator 完整 schema）
# ---------------------------------------------------------------------------

#: validator Shot.shot_type 的枚举值


def _adapt_shooting_checklist(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    from ...validator import validate_dict
    # Phase 5：先归一化为 validator 完整 schema 再校验（工具链简化格式直
    # 接校验会误报结构非法）；checklist 条目仍按原始简化数据组装。
    errors, _ = validate_dict(_normalize_storyboard_for_validation(p.storyboard))
    if errors:
        # 此前只截前 5 条，截图里只看到镜头 1、2 的问题，容易误以为只有这两镜有问题。
        suffix = f"（等共 {len(errors)} 条）" if len(errors) > 5 else ""
        return _fail("validation",
                     "分镜未通过校验: " + "; ".join(errors[:5]) + suffix,
                     data={"errors": errors})
    shots = p.storyboard.get("shots") or []
    shoot_state = p.shoot_state or {}
    done = set(shoot_state.get("done_shot_nos") or [])
    failed_shots = set(shoot_state.get("failed_shot_nos") or [])
    items = []
    for s in shots:
        no = s.get("shot_no")
        st = "done" if no in done else ("reshoot" if no in failed_shots else "todo")
        items.append({"shot_no": no, "scene": s.get("scene", ""),
                      "shot_type": s.get("shot_type", ""),
                      "duration_sec": s.get("duration_sec"),
                      "voiceover": s.get("voiceover", ""),
                      "shoot_notes": s.get("shoot_notes", ""),
                      "status": st})
    return _ok({"checklist": items,
                "todo_count": sum(1 for i in items if i["status"] == "todo"),
                "reshoot_count": sum(1 for i in items if i["status"] == "reshoot")})


def _adapt_rewrite_copy(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "copy:create")
    ai = _require_ai(deps, "rewrite_copy")
    prompt = (
        "把下面的书面口播文案改写成山东农人口语味（拉呱风：实在、唠嗑、不端着），"
        "返回 JSON：{\"rewritten\": str, \"changes\": [str]}。"
        "禁止编造产品事实，只能改写表达。"
        f"\n原文：{p.text}"
        + (f"\n产品名：{p.product_name}" if p.product_name else "")
    )
    try:
        result = ai(prompt)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except Exception as e:
        return _fail("model_error", f"文案改写模型调用失败: {e}")
    if not isinstance(result, dict) or not (result.get("rewritten") or "").strip():
        return _fail("model_error", "模型返回结构非法：缺少 rewritten 字段")
    return _ok({"rewritten": result["rewritten"],
                "changes": result.get("changes", [])})


def _adapt_check_delivery(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    storyboard = p.storyboard
    if storyboard is None and p.work_id:
        w = _get_work_owned(deps.db, deps.identity.org_id, p.work_id)
        try:
            storyboard = json.loads(w.data_json or "{}").get("storyboard")
        except (json.JSONDecodeError, TypeError):
            storyboard = None
    facts = FactRepository(deps.db).list_usable(deps.identity.org_id, limit=200)
    result = delivery_service.check_delivery(
        # Phase 5：归一化后再做交付检查，避免简化格式被误判"时长与校验"阻断
        storyboard=_normalize_storyboard_for_validation(storyboard or {}),
        facts=[{"id": f.id, "content": f.content, "status": f.status,
                "allow_use": f.allow_use} for f in facts],
        check_type=p.check_type,
    )
    return _ok({"delivery": result})


def _adapt_parse_brief(deps: ToolDeps, p) -> ToolResult:
    """对话理解/brief 解析 → Phase 5 brief.py（任务书 §5.1 完整 schema）。

    关键事实不明绝不臆造 → items_to_verify（待核实）；缺失一般字段用
    透明默认值并在 confirmed_defaults 标出。
    """
    _require_action(deps.db, deps.identity, "org:read")
    ai = _require_ai(deps, "parse_brief")
    from .brief import BriefParseError, parse_brief_from_text

    def _generate(prompt: str, feedback=None) -> dict:
        # ToolDeps.ai_generate 是 prompt→dict 单轮协议；修复轮把 feedback
        # 拼进 prompt（brief.py 的受控修复语义保持不变）
        if feedback:
            prompt = f"{prompt}\n\n修正要求：{feedback}"
        return ai(prompt)

    try:
        brief = parse_brief_from_text(p.user_text, _generate)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except BriefParseError as e:
        return _fail("model_error", str(e))
    except Exception as e:  # noqa: BLE001
        return _fail("model_error", f"brief 解析模型调用失败: {e}")
    return _ok({"brief": brief.model_dump()})


def _adapt_generate_strategy(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    ai = _require_ai(deps, "generate_strategy")
    facts_text = ""
    if p.inject_confirmed_facts:
        facts = FactRepository(deps.db).list_usable(
            deps.identity.org_id, product_id=p.product_id, limit=20)
        facts_text = "\n".join(f"- {f.content}" for f in facts)
    prompt = (
        "你是农产品短视频营销策略师。根据以下 brief 制定营销策略与内容目标，"
        "返回 JSON：{\"strategy\": {\"goals\": [str], "
        "\"target_audience\": str, \"content_pillars\": [str], "
        "\"key_messages\": [str]}}。"
        "要求：目标可衡量、支柱 2-4 个、关键信息紧扣已确认事实，"
        "禁止编造产地/认证/功效等关键事实。"
        f"\nbrief：{p.brief}"
        + (f"\n已确认事实（只能引用这些，禁止编造）：\n{facts_text}" if facts_text else "")
    )
    try:
        result = ai(prompt)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except Exception as e:
        return _fail("model_error", f"策略生成模型调用失败: {e}")
    strategy = result.get("strategy") if isinstance(result, dict) else None
    if not isinstance(strategy, dict) or not strategy.get("goals"):
        return _fail("model_error", "模型返回结构非法：缺少 strategy.goals")
    return _ok({"strategy": strategy},
               warnings=["结果未落盘：如需保存，请用 patch_work 写回作品"])


def _adapt_generate_platform_copy(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "copy:create")
    ai = _require_ai(deps, "generate_platform_copy")
    prompt = (
        f"你是农产品短视频平台文案写手。为【{p.platform}】平台撰写发布文案，"
        "返回 JSON：{\"copy\": {\"platform\": str, \"title\": str, "
        "\"text\": str, \"hashtags\": [str]}}。"
        "要求：标题抓人、正文口语化带卖点、3-8 个话题标签；"
        "禁止编造产地/认证/功效等关键事实；不确定的写成\"待核实\"。"
        f"\nbrief：{p.brief}"
        + (f"\n创意方向：{p.creative}" if p.creative else "")
        + (f"\n口播文案：{p.voiceover}" if p.voiceover else "")
        + (f"\n产品名：{p.product_name}" if p.product_name else "")
    )
    try:
        result = ai(prompt)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except Exception as e:
        return _fail("model_error", f"平台文案生成模型调用失败: {e}")
    copy = result.get("copy") if isinstance(result, dict) else None
    if not isinstance(copy, dict) or not (copy.get("text") or "").strip():
        return _fail("model_error", "模型返回结构非法：缺少 copy.text")
    copy["platform"] = p.platform
    return _ok({"copy": copy},
               warnings=["结果未落盘：如需保存，请用 patch_work 写回作品"])


def _adapt_patch_work(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:update")
    w = _get_work_owned(deps.db, deps.identity.org_id, p.work_id)
    try:
        current = json.loads(w.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return _fail("internal", "作品 data_json 已损坏，无法做局部更新")
    if not isinstance(current, dict):
        return _fail("internal", "作品 data_json 不是对象，无法做局部更新")
    merged = dict(current)
    if p.data_patch:
        illegal = [k for k in p.data_patch if k not in PATCHABLE_DATA_KEYS]
        if illegal:
            return _fail("invalid_params",
                         f"data_patch 含非法键（只允许 {sorted(PATCHABLE_DATA_KEYS)}）: {illegal}")
        for k, v in p.data_patch.items():
            merged[k] = v
    if not p.title and not p.data_patch and p.status is None:
        return _fail("invalid_params", "title/data_patch/status 至少给一个")
    try:
        updated = WorkRepository(deps.db).update(
            p.work_id, deps.identity.org_id,
            expected_version=p.expected_version,
            title=p.title, data=merged, status=p.status)
    except OptimisticLockError as e:
        raise _Conflict(str(e))
    except EntityNotFound:
        raise _NotFound("作品不存在")
    return _ok({"work_id": updated.id, "version": updated.version,
                "title": updated.title, "status": updated.status},
               artifact_ref=f"work:{updated.id}:v{updated.version}")


def _adapt_get_task_status(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "org:read")
    t = deps.db.query(models.AITask).filter_by(
        id=p.task_id, org_id=deps.identity.org_id).first()
    if t is None:
        raise _NotFound("任务不存在")
    return _ok({"task": {
        "id": t.id, "task_type": t.task_type, "status": t.status,
        "progress": t.progress, "error": t.error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "finished_at": (t.finished_at.isoformat()
                        if getattr(t, "finished_at", None) else None),
    }})


def _adapt_query_usage(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "org:read")
    q = ai_ledger.org_calls_query(deps.db, deps.identity.org_id)
    if p.task_id:
        q = q.filter(models.AICall.task_id == p.task_id)
    total_calls, total_tokens, total_cost = q.with_entities(
        func.count(models.AICall.id),
        func.coalesce(func.sum(models.AICall.total_tokens), 0),
        func.coalesce(func.sum(models.AICall.cost_cents), 0),
    ).one()
    recent = (q.order_by(models.AICall.created_at.desc())
               .limit(p.limit).all())
    return _ok({
        "summary": {"calls": total_calls, "total_tokens": int(total_tokens),
                    "cost_cents": int(total_cost)},
        "recent": [{
            "id": c.id, "task_id": c.task_id, "task_type": c.task_type,
            "model": c.model, "total_tokens": c.total_tokens,
            "cost_cents": c.cost_cents, "status": c.status,
        } for c in recent],
    })


def _adapt_regenerate_shot(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:read")
    w = _get_work_owned(deps.db, deps.identity.org_id, p.work_id)
    sb_repo = StoryboardRepository(deps.db)
    sb = sb_repo.get(p.storyboard_id)
    if sb is None or sb.work_id != w.id:
        raise _NotFound("分镜不存在")
    locked = sb_repo.list_locked_shot_nos(p.storyboard_id)
    if p.shot_no in locked:
        return _fail("locked", f"第 {p.shot_no} 个镜头已锁定，不许改动")
    ai = _require_ai(deps, "regenerate_shot")
    shots = sb_repo.list_shots(p.storyboard_id)
    old = next((s for s in shots if getattr(s, "shot_no", None) == p.shot_no),
               None)
    prompt = (
        "按指令重写单个分镜镜头，返回 JSON："
        "{\"shot_no\": int, \"scene\": str, \"shot_type\": str, "
        "\"duration_sec\": number, \"voiceover\": str, \"subtitle\": str, "
        "\"shoot_notes\": str}。"
        f"\n重写第 {p.shot_no} 个镜头。指令：{p.instruction}"
        + (f"\n原镜头：{json.dumps({'scene': old.scene, 'voiceover': getattr(old, 'voiceover', '')}, ensure_ascii=False)}"
           if old is not None else "")
    )
    try:
        result = ai(prompt)
    except _CONTROL_FLOW_EXCEPTIONS:
        raise
    except Exception as e:
        return _fail("model_error", f"镜头重生成模型调用失败: {e}")
    if not isinstance(result, dict) or not result.get("scene"):
        return _fail("model_error", "模型返回结构非法：缺少 scene 字段")
    result["shot_no"] = p.shot_no
    return _ok({"shot": result},
               artifact_ref=f"storyboard:{p.storyboard_id}:shot:{p.shot_no}",
               warnings=["结果未落盘：如需保存，请用 patch_work 写回作品"])


def _adapt_delete_work(deps: ToolDeps, p) -> ToolResult:
    _require_action(deps.db, deps.identity, "work:delete")
    w = _get_work_owned(deps.db, deps.identity.org_id, p.work_id)
    WorkRepository(deps.db).soft_delete(p.work_id, deps.identity.org_id)
    return _ok({"work_id": w.id, "deleted": True},
               artifact_ref=f"work:{w.id}:deleted")


_ADAPTERS: dict[str, Callable[[ToolDeps, Any], ToolResult]] = {
    "query_facts": _adapt_query_facts,
    "query_products": _adapt_query_products,
    "generate_ideas": _adapt_generate_ideas,
    "generate_storyboard": _adapt_generate_storyboard,
    "generate_strategy": _adapt_generate_strategy,
    "generate_platform_copy": _adapt_generate_platform_copy,
    "check_risk": _adapt_check_risk,
    "shooting_checklist": _adapt_shooting_checklist,
    "rewrite_copy": _adapt_rewrite_copy,
    "check_delivery": _adapt_check_delivery,
    "parse_brief": _adapt_parse_brief,
    "patch_work": _adapt_patch_work,
    "get_task_status": _adapt_get_task_status,
    "query_usage": _adapt_query_usage,
    "regenerate_shot": _adapt_regenerate_shot,
    "delete_work": _adapt_delete_work,
}


def run_tool(deps: ToolDeps, tool_id: str, params: dict[str, Any],
             confirmed: bool = False) -> ToolResult:
    """统一分派入口：白名单 → 参数校验 → 确认门 → 权限 → 执行 → 结构化错误。"""
    defn: Optional[ToolDefinition] = get_tool(tool_id)
    if defn is None or not is_tool_allowed(tool_id):
        return _fail("unknown_tool", f"tool_id 不在白名单: {tool_id}")
    try:
        validated = validate_tool_params(tool_id, params)
    except KeyError:
        return _fail("unknown_tool", f"tool_id 不在白名单: {tool_id}")
    except ToolParamError as e:
        return _fail("invalid_params", str(e))
    if defn.tier == ToolTier.USER_CONFIRMATION_REQUIRED and not confirmed:
        return _fail("needs_confirmation",
                     f"工具 {tool_id} 为破坏性操作，需用户确认后执行")
    adapter = _ADAPTERS.get(tool_id)
    if adapter is None:
        return _fail("internal", f"工具 {tool_id} 已注册但无适配器实现")
    try:
        result = adapter(deps, validated)
    except _CONTROL_FLOW_EXCEPTIONS:
        # 控制流异常（模型步数超限 / 429 硬停）必须穿透，不转结构化错误
        raise
    except _ToolNeedsModel as e:
        return _fail("needs_model", str(e))
    except _Forbidden as e:
        return _fail("forbidden", str(e))
    except _NotFound as e:
        return _fail("not_found", str(e))
    except _Conflict as e:
        return _fail("conflict", str(e))
    except (EntityNotFound,) as e:
        return _fail("not_found", str(e))
    except OptimisticLockError as e:
        return _fail("conflict", str(e))
    except Exception as e:  # 异常一律转结构化错误，不抛原始堆栈
        return _fail("internal", f"{type(e).__name__}: {e}")
    if not isinstance(result, ToolResult):
        return _fail("internal", f"适配器 {tool_id} 返回非法信封")
    return result
