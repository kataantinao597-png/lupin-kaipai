"""Phase 6：连续对话与局部修改（任务书 §十）。

自然语言修改请求 → ModificationPlan（意图解析：网关模型 + schema 校验，
≤2 次受控修复，沿用 brief.py 的修复模式）→ 影响分析（受影响的
artifact / 镜头 / 检查项，写入 agent_steps）→ 保护规则（锁定镜头 /
人工确认事实 / 大范围覆盖 / 高风险变化 → waiting_user 确认，确认机制
复用 runner.confirm_step；普通局部改写自动执行）→ 修改前 WorkVersion
快照（语义复用 backend/api/v1/works.py::_snapshot_work 091，本模块镜像
实现以避免 service→api 反向依赖）→ patch 语义执行（只写目标字段 /
目标 artifact，经 patch_work + PATCHABLE_DATA_KEYS + Work.version
乐观锁）→ 改后重检（时长 / 可行性 / 事实引用 / 风险 / 交付，调既有
service，结果写入 step）→ 结构化修改摘要 + 一键恢复。

跨作品保护：修改目标 work_id 与 conversation 的 work_id 不一致时拒绝
（EntityNotFound → 404，不执行任何写操作）。

红线：真实模型调用只走保险箱 luna/gpt（经 gateway），代码不写 key；
429 撞线即硬停（RateLimitedStop 穿透）；视频 key 绝不碰。
"""
from __future__ import annotations

# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入，先走 backend.auth.*。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ...ai_tasks.repository import AITaskRepository
from ...models import Membership, WorkVersion
from ...rbac import has_permission
from ...risk import rule_scan
from ...repositories.base import EntityNotFound
from ...repositories.fact_repository import FactRepository
from ...repositories.storyboard_repository import StoryboardRepository
from ...repositories.work_repository import WorkRepository
from .adapters import RateLimitedStop, ToolDeps, ToolResult, run_tool
from .context import AgentIdentity
from .conversations import (
    AgentStep,
    append_message,
    get_conversation_or_404,
)
from .quality_gates import check_fact_grounding
from .runner import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_USER,
    confirm_step,
    create_agent_run,
    step_params_digest,
)
from .tools import PATCHABLE_DATA_KEYS

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_MODIFY_REPAIRS",
    "MAX_AUTO_ARTIFACTS",
    "MAX_VERSIONS_PER_WORK",
    "ModificationPlan",
    "ModificationImpact",
    "ModifyParseError",
    "ModifyExecutionError",
    "parse_modification_from_text",
    "analyze_impact",
    "protection_reason",
    "apply_modification",
    "confirm_modification",
    "restore_modification",
]

#: 意图解析的受控修复上限（初次 + 2 次修复 = 3 轮上限，任务书红线）
MAX_MODIFY_REPAIRS = 2

#: 自动执行的 artifact 上限：一次修改影响的 artifact 数 > 3 → 先确认
MAX_AUTO_ARTIFACTS = 3

#: 单个作品的版本快照保留上限（与 works.py 091 一致）
MAX_VERSIONS_PER_WORK = 50

#: 人工确认事实相关的目标字段：改这些 = 改人工确认的事实 → 先确认
CONFIRMED_FACT_FIELDS = frozenset({
    "brief.product_name",
    "brief.product_id",
    "brief.location",
})

#: 重检项 → 触发它的 artifact 映射
CHECKS_BY_ARTIFACT: dict[str, list[str]] = {
    "storyboard": ["duration", "feasibility", "fact_grounding",
                   "risk", "delivery"],
    "copy": ["fact_grounding", "risk", "delivery"],
    "brief": ["feasibility", "delivery"],
    "creative": ["fact_grounding", "risk", "delivery"],
    "strategy": ["delivery"],
    "shooting_list": ["duration", "feasibility", "delivery"],
}


class ModifyParseError(ValueError):
    """修改意图解析失败：超过修复上限仍未得到合法结构。"""


class ModifyExecutionError(RuntimeError):
    """修改执行失败（快照/写回/重检阶段的结构化错误）。"""


class ModificationPlan(BaseModel):
    """任务书 §十的修改计划。

    target 约定：
    - target_kind="artifact" → target 为 artifact 名
      （brief/storyboard/copy/strategy/creative/shooting_list）
    - target_kind="shot" → target 为 "shot:3" 或 "shot"（多镜头时看
      params.shot_nos）
    - target_kind="field" → target 为点路径（如 "brief.budget_yuan"）

    operation：
    - rewrite：按 params.instruction 用模型重写内容
    - replace：按 params.updates / params.value 直接替换字段值（无模型）
    - delete：按 params.shot_nos / params.fields 删除
    - explain：解释风险并给出更稳妥文案（params.text 为风险原文）
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    target_kind: Literal["artifact", "shot", "field"] = Field()
    target: str = Field(min_length=1, max_length=128)
    operation: Literal["rewrite", "replace", "delete", "explain"] = Field()
    params: dict[str, Any] = Field(default_factory=dict)
    scope: Literal["local"] = Field(default="local")
    touches_confirmed_fact: bool = Field(
        default=False,
        description="模型判断：该修改是否涉及人工已确认事实 "
                    "(产品名/产地/认证/价格等)")

    @field_validator("params", mode="before")
    @classmethod
    def _coerce_params(cls, v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}


class ModificationImpact(BaseModel):
    """影响分析结果：受影响的 artifact / 镜头 / 需重跑的检查项。"""

    model_config = ConfigDict(extra="ignore")

    impacted_artifacts: list[str] = Field(default_factory=list)
    impacted_shots: list[int] = Field(default_factory=list)
    checks_to_rerun: list[str] = Field(default_factory=list)
    locked_shots_hit: list[int] = Field(default_factory=list)
    touches_confirmed_fact: bool = False
    artifact_count: int = 0


_PLAN_FIELDS_HINT = (
    "target_kind(\"artifact\"|\"shot\"|\"field\"), target, "
    "operation(\"rewrite\"|\"replace\"|\"delete\"|\"explain\"), params(对象), "
    "scope(\"local\"), touches_confirmed_fact(true|false)"
)

_SYSTEM_RULES = (
    "你是「鲁品开拍」AI 助手的修改意图解析器。把用户的修改请求解析为结构化"
    "修改计划，只输出纯 JSON（不要 markdown、不要解释），字段："
    + _PLAN_FIELDS_HINT + "。"
    "target/params 约定："
    "1) 改风格/整体重写某个 artifact → target_kind=\"artifact\"，"
    "target 为 artifact 名（brief/storyboard/copy/strategy/creative/"
    "shooting_list），operation=\"rewrite\"，"
    "params={\"instruction\": \"用户原话改写后的执行指令\", "
    "\"fields\": [\"要改的字段名\"]}；"
    "2) 改预算/人数/时长等具体数值 → target_kind=\"field\"，"
    "target 为点路径（如 \"brief.budget_yuan\"），operation=\"replace\"，"
    "params={\"value\": 新值}；一次改多个字段时 params={\"updates\": "
    "{\"brief.budget_yuan\": 300, \"brief.people_count\": 1}}（绝对路径）；"
    "3) 换设备相关镜头 → target_kind=\"shot\"，target=\"shot\"，"
    "operation=\"rewrite\"，params={\"shot_nos\": [命中的镜头号], "
    "\"instruction\": \"去掉XX设备的拍摄方式，换成…\"}；"
    "镜头号按上下文中给出的分镜现状判定（如 shoot_notes 含\"稳定器\"的镜头）；"
    "4) 只重做第 N 个镜头 → target_kind=\"shot\"，target=\"shot:N\"，"
    "operation=\"rewrite\"，params={\"instruction\": \"…\"}；"
    "5) 解释风险并给稳妥文案 → operation=\"explain\"，"
    "target_kind=\"artifact\"/\"field\"/\"shot\"按风险文本所在位置填，"
    "params={\"text\": \"风险原文\", \"context\": \"风险说明\"}；"
    "6) scope 恒为 \"local\"；"
    "7) touches_confirmed_fact：修改涉及产品名/产地/认证/功效/价格/销量等"
    "人工已确认事实时为 true，否则 false；拿不准时填 true（宁可多确认）。"
    "典型映射示例："
    "「整体风格改得更有山东本土特色」→ artifact/brief/rewrite/fields=[\"style\"]；"
    "「预算改为300元，拍摄人员只有1人」→ field/replace/updates；"
    "「口播压缩到30秒」→ artifact/storyboard/rewrite + "
    "params.field_updates={\"brief.video_duration_sec\": 30}；"
    "「解释这个风险，并根据已确认事实给出更稳妥的文案」→ explain。"
)


def _make_parse_prompt(user_text: str, context: Optional[dict[str, Any]],
                       feedback: Optional[str]) -> str:
    ctx_lines: list[str] = []
    if context:
        if context.get("work_title"):
            ctx_lines.append(f"作品标题：{context['work_title']}")
        arts = context.get("artifacts") or []
        if arts:
            ctx_lines.append(f"现有 artifact：{', '.join(arts)}")
        sb = context.get("storyboard_summary") or ""
        if sb:
            ctx_lines.append(f"分镜现状：{sb}")
        locked = context.get("locked_shots") or []
        if locked:
            ctx_lines.append(
                f"锁定镜头：{', '.join(str(x) for x in locked)}"
                "（锁定镜头不许改动）")
        facts = context.get("confirmed_facts") or []
        if facts:
            ctx_lines.append("已确认可用事实："
                             + "；".join(str(f)[:60] for f in facts[:10]))
        recent = context.get("recent_messages") or []
        if recent:
            ctx_lines.append("最近对话：" + " | ".join(
                str(m)[:120] for m in recent[-4:]))
    ctx_text = "\n".join(ctx_lines)
    prompt = (f"【修改意图解析】\n{_SYSTEM_RULES}\n"
              + (f"上下文：\n{ctx_text}\n" if ctx_text else "")
              + f"用户修改请求：{user_text}")
    if feedback:
        prompt += f"\n\n上次输出未通过校验，请修正后重新输出完整 JSON：\n{feedback}"
    return prompt


def parse_modification_from_text(
    user_text: str,
    generate: Callable[[str, Optional[str]], dict[str, Any]],
    *,
    context: Optional[dict[str, Any]] = None,
    max_repairs: int = MAX_MODIFY_REPAIRS,
) -> ModificationPlan:
    """自然语言修改请求 → ModificationPlan。

    generate(prompt, feedback) -> dict（prompt→解析后 JSON）；
    JSON/结构非法时做 ≤max_repairs 次受控修复，仍失败抛 ModifyParseError。
    """
    text = (user_text or "").strip()
    if not text:
        raise ModifyParseError("用户输入为空，无法解析修改意图")
    last_errors: list[str] = []
    feedback: Optional[str] = None
    for _ in range(max_repairs + 1):
        try:
            data = generate(_make_parse_prompt(text, context, feedback),
                            feedback)
        except Exception as e:  # noqa: BLE001  模型调用异常直接失败
            # 429 硬停必须穿透，不被吞成解析失败
            if isinstance(e, RateLimitedStop):
                raise
            raise ModifyParseError(f"修改意图解析模型调用失败: {e}") from e
        if not isinstance(data, dict):
            last_errors = ["模型输出不是 JSON 对象"]
            feedback = "上次输出不是合法 JSON 对象，请只输出纯 JSON。"
            continue
        try:
            plan = ModificationPlan.model_validate(data)
        except Exception as e:  # pydantic ValidationError
            last_errors = [str(e)[:500]]
            feedback = f"字段校验失败：{last_errors[0]}"
            continue
        return plan
    raise ModifyParseError(
        "修改意图解析失败（已达修复上限）："
        + "; ".join(last_errors[:3]))


def _targeted_artifacts(plan: ModificationPlan) -> list[str]:
    arts: list[str] = []
    if plan.target_kind == "artifact":
        arts.append(plan.target)
    elif plan.target_kind == "field":
        arts.append(plan.target.split(".")[0])
    elif plan.target_kind == "shot":
        arts.append("storyboard")
    for k in (plan.params.get("updates") or {}):
        if isinstance(k, str) and "." in k:
            arts.append(k.split(".")[0])
    for k in (plan.params.get("field_updates") or {}):
        if isinstance(k, str) and "." in k:
            arts.append(k.split(".")[0])
    # 去重保序；只保留白名单内的 artifact 键
    seen: set[str] = set()
    out: list[str] = []
    for a in arts:
        if a in PATCHABLE_DATA_KEYS and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _targeted_shots(plan: ModificationPlan,
                    data: dict[str, Any]) -> list[int]:
    if plan.target_kind != "shot":
        return []
    nos = plan.params.get("shot_nos")
    if isinstance(nos, list) and nos:
        return sorted({int(n) for n in nos if str(n).isdigit()})
    if plan.target.startswith("shot:"):
        tail = plan.target.split("shot:", 1)[1].split(",")[0].strip()
        if tail.isdigit():
            return [int(tail)]
    return []


def _locked_shot_nos(db: Session, work_id: str) -> list[int]:
    """复用 StoryboardRepository 的锁定镜头检查（Shot.locked）。"""
    repo = StoryboardRepository(db)
    locked: set[int] = set()
    for sb in repo.list_by_work(work_id):
        locked.update(repo.list_locked_shot_nos(sb.id))
    return sorted(locked)


def _storyboard_summary(data: dict[str, Any]) -> str:
    sb = data.get("storyboard") or {}
    shots = sb.get("shots") or []
    parts = []
    for s in shots[:12]:
        parts.append(
            f"第{s.get('shot_no')}镜[{s.get('shot_type', '')}]"
            f"{str(s.get('scene') or '')[:20]}"
            f"/{str(s.get('shoot_notes') or '')[:24]}")
    return "；".join(parts)


def analyze_impact(db: Session, plan: ModificationPlan,
                   data: dict[str, Any], work_id: str) -> ModificationImpact:
    """计算修改的影响范围：artifact / 镜头 / 需重跑的检查项。"""
    arts = _targeted_artifacts(plan)
    shots = _targeted_shots(plan, data)
    locked = _locked_shot_nos(db, work_id)
    locked_hit = [n for n in shots if n in locked]
    checks: list[str] = []
    for a in arts:
        for c in CHECKS_BY_ARTIFACT.get(a, []):
            if c not in checks:
                checks.append(c)
    fact_flag = bool(plan.touches_confirmed_fact) or any(
        plan.target == f or plan.target.startswith(f + ".")
        or (isinstance((plan.params.get("updates") or {}), dict)
            and f in (plan.params.get("updates") or {}))
        for f in CONFIRMED_FACT_FIELDS)
    return ModificationImpact(
        impacted_artifacts=arts,
        impacted_shots=shots,
        checks_to_rerun=checks,
        locked_shots_hit=locked_hit,
        touches_confirmed_fact=fact_flag,
        artifact_count=len(arts),
    )


def protection_reason(plan: ModificationPlan,
                      impact: ModificationImpact) -> Optional[str]:
    """保护规则（静态部分）：返回需确认的原因，无需确认返回 None。

    - 锁定镜头命中 → 确认
    - 人工确认事实的修改 → 确认
    - 大范围覆盖（artifact 数 > MAX_AUTO_ARTIFACTS）→ 确认
    高风险变化在内容生成后另行判定（见 _risk_reason_on_new_data）。
    """
    if impact.locked_shots_hit:
        return ("shot_locked:第 %s 个镜头已锁定，需人工确认后才能改动"
                % "、".join(str(n) for n in impact.locked_shots_hit))
    if impact.touches_confirmed_fact:
        return "confirmed_fact:该修改涉及人工已确认事实，需人工确认"
    if impact.artifact_count > MAX_AUTO_ARTIFACTS:
        return (f"wide_scope:本次修改影响 {impact.artifact_count} 个 artifact"
                f"（{', '.join(impact.impacted_artifacts)}），"
                f"超过自动执行上限 {MAX_AUTO_ARTIFACTS}，需人工确认")
    return None


def _snapshot_work_version(db: Session, work: Any,
                           user_id: str) -> WorkVersion:
    """修改前存快照。

    语义复用 backend/api/v1/works.py::_snapshot_work（091）：
    version_no 单调递增；超 MAX_VERSIONS_PER_WORK 删最旧。
    此处镜像实现，避免 service 层反向依赖 api 层。
    """
    last = (db.query(WorkVersion)
              .filter(WorkVersion.work_id == work.id)
              .order_by(WorkVersion.version_no.desc())
              .first())
    next_no = (last.version_no + 1) if last else 1
    snap = WorkVersion(
        work_id=work.id,
        version_no=next_no,
        snapshot_json=work.data_json or "{}",
        title=work.title or "",
        created_by=user_id,
    )
    db.add(snap)
    db.flush()
    excess = (db.query(WorkVersion)
                .filter(WorkVersion.work_id == work.id)
                .order_by(WorkVersion.version_no.asc())
                .offset(MAX_VERSIONS_PER_WORK)
                .all())
    for old in excess:
        db.delete(old)
    db.commit()
    return snap


def restore_modification(db: Session, user: Any, org_id: str, work_id: str,
                         snapshot_version_no: int) -> dict[str, Any]:
    """一键恢复：回到修改前的快照版本。

    语义复用 works.py::restore_version（091）：恢复前先把当前状态存档，
    使恢复操作本身也可撤销。work:update 权限必需。
    """
    user_id = _normalize_user_id(user)
    ms = (db.query(Membership)
            .filter_by(user_id=user_id, org_id=org_id).first())
    if ms is None:
        raise EntityNotFound("Organization", org_id)
    if not has_permission(ms.role, "work:update"):
        raise EntityNotFound("Work", work_id)  # 404 防枚举
    work = WorkRepository(db).get(work_id, org_id)
    if work is None:
        raise EntityNotFound("Work", work_id)
    ver = (db.query(WorkVersion)
             .filter(WorkVersion.work_id == work_id,
                     WorkVersion.version_no == snapshot_version_no)
             .first())
    if ver is None:
        raise EntityNotFound("WorkVersion", str(snapshot_version_no))
    _snapshot_work_version(db, work, user_id)  # 当前状态先存档
    work.data_json = ver.snapshot_json
    work.title = ver.title
    db.commit()
    db.refresh(work)
    logger.info("作品 %s 已恢复到快照 v%s", work_id, snapshot_version_no)
    return {"work_id": work_id, "version_no": snapshot_version_no,
            "restored": True, "title": work.title}

def _normalize_user_id(user: Any) -> str:
    if isinstance(user, str):
        return user
    for attr in ("user_id", "id"):
        v = getattr(user, attr, None)
        if v:
            return str(v)
    raise ValueError("无法从 user 解析 user_id")


def _needs_generation(plan: ModificationPlan) -> bool:
    return plan.operation in ("rewrite", "explain")


def _make_generate_prompt(plan: ModificationPlan,
                          data: dict[str, Any]) -> str:
    if plan.operation == "explain":
        text = plan.params.get("text", "")
        ctx = plan.params.get("context", "")
        return (
            "【风险解释】\n你是农产品短视频风控文案助手。解释下面这条风险，"
            "并根据已确认事实给出更稳妥的替代文案。只输出纯 JSON："
            "{\"explanation\": \"风险解释（≤200字）\", "
            "\"safer_text\": \"稳妥替代文案\"}。"
            "规则：不编造产地/认证/功效/价格/销量等关键事实；"
            "不确定的写成\"待核实\"。"
            f"\n风险原文：{text}"
            + (f"\n风险说明：{ctx}" if ctx else "")
        )
    if plan.target_kind == "shot":
        shots = (data.get("storyboard") or {}).get("shots") or []
        nos = _targeted_shots(plan, data)
        cur = [s for s in shots if s.get("shot_no") in nos]
        return (
            "【局部重写·镜头】\n你是分镜师。按指令重写指定镜头，只输出纯 JSON："
            "{\"shots\": [{\"shot_no\": int, \"scene\": str, "
            "\"shot_type\": str, \"duration_sec\": number, "
            "\"voiceover\": str, \"subtitle\": str, "
            "\"shoot_notes\": str}]}（有几个镜头就返回几个）。"
            "规则：只改指令要求的部分；不编造关键事实；"
            "shot_type 只能是 特写/近景/中景/中远景/全景/远景 之一。"
            f"\n指令：{plan.params.get('instruction', '')}"
            f"\n当前镜头：{json.dumps(cur, ensure_ascii=False)}"
        )
    artifact = data.get(plan.target) or {}
    fields = plan.params.get("fields") or []
    return (
        "【局部重写】\n你是修改执行器。按指令重写目标内容，只输出纯 JSON："
        "{\"updates\": {字段名: 新值}}。"
        "规则：只改指令要求的部分（fields 列表），其他字段保持原样；"
        "不编造关键事实；保持原有 JSON 结构。"
        f"\n指令：{plan.params.get('instruction', '')}"
        + (f"\n限定字段：{', '.join(str(f) for f in fields)}" if fields else "")
        + f"\n当前内容：{json.dumps(artifact, ensure_ascii=False)[:3000]}"
    )


def _validate_generated(plan: ModificationPlan,
                        generated: dict[str, Any]) -> dict[str, Any]:
    """校验模型生成内容的结构；非法抛 ModifyExecutionError。"""
    if not isinstance(generated, dict):
        raise ModifyExecutionError("模型生成内容不是 JSON 对象")
    if plan.operation == "explain":
        if not (generated.get("safer_text") or "").strip():
            raise ModifyExecutionError("风险解释返回缺少 safer_text")
        return generated
    if plan.target_kind == "shot":
        shots = generated.get("shots")
        if not isinstance(shots, list) or not shots:
            raise ModifyExecutionError("镜头重写返回缺少 shots 数组")
        return generated
    updates = generated.get("updates")
    if not isinstance(updates, dict) or not updates:
        raise ModifyExecutionError("重写返回缺少 updates 对象")
    return generated


def _set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    """按点路径设值（只写目标字段），中间层不存在时建 dict。"""
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _compute_data_patch(plan: ModificationPlan, data: dict[str, Any],
                        generated: Optional[dict[str, Any]]) -> dict[str, Any]:
    """计算 patch_work 的 data_patch：只含目标 artifact 键。

    返回 {artifact_key: 合并后的新 artifact}；调用方与旧 data 合并后即为
    完整新 data（未触及的 artifact 原样保留）。
    """
    patch: dict[str, Any] = {}

    def _artifact(key: str) -> dict[str, Any]:
        a = data.get(key)
        return dict(a) if isinstance(a, dict) else {}

    if plan.operation == "replace":
        updates = dict(plan.params.get("updates") or {})
        if "value" in plan.params and plan.target_kind == "field":
            updates[plan.target] = plan.params["value"]
        if not updates:
            raise ModifyExecutionError("replace 操作缺少 updates/value")
        for abspath, v in updates.items():
            if not isinstance(abspath, str) or "." not in abspath:
                raise ModifyExecutionError(f"非法字段路径: {abspath!r}")
            top = abspath.split(".")[0]
            art = patch.get(top)
            if art is None:
                art = _artifact(top)
                patch[top] = art
            _set_path(art, abspath[len(top) + 1:], v)
    elif plan.operation == "rewrite" and generated is not None:
        if plan.target_kind == "shot":
            sb = _artifact("storyboard")
            shots = [dict(s) for s in (sb.get("shots") or [])]
            new_by_no = {int(s["shot_no"]): s
                         for s in generated["shots"]
                         if str(s.get("shot_no")).isdigit()}
            for i, s in enumerate(shots):
                no = s.get("shot_no")
                if no in new_by_no:
                    merged = dict(s)
                    merged.update(new_by_no[no])
                    merged["shot_no"] = no
                    shots[i] = merged
            sb["shots"] = shots
            patch["storyboard"] = sb
        else:
            art = _artifact(plan.target)
            art.update(generated["updates"])
            patch[plan.target] = art
        for abspath, v in (plan.params.get("field_updates") or {}).items():
            if not isinstance(abspath, str) or "." not in abspath:
                continue
            top = abspath.split(".")[0]
            art = patch.get(top)
            if art is None:
                art = _artifact(top)
                patch[top] = art
            _set_path(art, abspath[len(top) + 1:], v)
    elif plan.operation == "explain" and generated is not None:
        safer = generated["safer_text"]
        if plan.target_kind == "shot":
            sb = _artifact("storyboard")
            shots = [dict(s) for s in (sb.get("shots") or [])]
            nos = _targeted_shots(plan, data) or [
                s.get("shot_no") for s in shots]
            for s in shots:
                if s.get("shot_no") in nos:
                    s["voiceover"] = safer
            sb["shots"] = shots
            patch["storyboard"] = sb
        elif plan.target_kind == "field":
            top = plan.target.split(".")[0]
            art = _artifact(top)
            _set_path(art, plan.target[len(top) + 1:], safer)
            patch[top] = art
        else:
            art = _artifact(plan.target)
            if "text" in art:
                art["text"] = safer
            elif "voiceover" in art:
                art["voiceover"] = safer
            else:
                art["safer_text"] = safer
            patch[plan.target] = art
    elif plan.operation == "delete":
        if plan.target_kind == "shot":
            nos = set(_targeted_shots(plan, data))
            if not nos and plan.params.get("shot_nos"):
                nos = {int(n) for n in plan.params["shot_nos"]
                       if str(n).isdigit()}
            sb = _artifact("storyboard")
            sb["shots"] = [s for s in (sb.get("shots") or [])
                           if s.get("shot_no") not in nos]
            patch["storyboard"] = sb
        else:
            fields = plan.params.get("fields") or []
            top = (plan.target.split(".")[0] if plan.target_kind == "field"
                   else plan.target)
            art = _artifact(top)
            for f in fields:
                art.pop(str(f).split(".")[-1], None)
            patch[top] = art
    else:
        raise ModifyExecutionError(
            f"不支持的操作组合: {plan.target_kind}/{plan.operation}")

    illegal = [k for k in patch if k not in PATCHABLE_DATA_KEYS]
    if illegal:
        raise ModifyExecutionError(f"patch 命中非白名单键: {illegal}")
    if not patch:
        raise ModifyExecutionError("计算出的 data_patch 为空，未执行写操作")
    return patch


def _recheck_duration(data: dict[str, Any]) -> dict[str, Any]:
    sb = data.get("storyboard") or {}
    shots = sb.get("shots") or []
    total = sum(float(s.get("duration_sec") or 0) for s in shots
                if isinstance(s, dict))
    target = (data.get("brief") or {}).get("video_duration_sec") or 0
    passed = True
    detail = f"总时长 {total:.0f}s"
    if target > 0 and total > 0 and abs(total - target) / target > 0.3:
        passed = False
        detail += f"，与目标 {target}s 偏差超 30%"
    return {"check": "duration", "passed": passed,
            "total_sec": round(total, 1), "target_sec": target,
            "detail": detail}


def _recheck_feasibility(data: dict[str, Any]) -> dict[str, Any]:
    """人员/设备/预算可行性（与 orchestrator._check_shoot_constraints 同口径）。"""
    brief = data.get("brief") or {}
    issues: list[str] = []
    people = brief.get("people_count")
    if people is not None and int(people) < 1:
        issues.append("拍摄人员不足 1 人")
    devices = set(brief.get("devices") or [])
    sl = data.get("shooting_list") or {}
    need = {d for i in (sl.get("items") or [])
            for d in (i.get("devices_needed") or [])}
    missing = sorted(need - devices)
    if missing:
        issues.append(f"缺少设备：{'、'.join(missing)}")
    budget = brief.get("budget_yuan")
    if budget is not None and float(budget) < 500:
        issues.append(f"预算仅 {budget} 元，建议最小配置拍摄")
    return {"check": "feasibility", "passed": not issues,
            "issues": issues,
            "detail": "通过" if not issues else "; ".join(issues)}


def _recheck_fact_grounding(data: dict[str, Any],
                            facts: list[dict[str, Any]]) -> dict[str, Any]:
    brief = data.get("brief") or {}
    artifacts = {k: data.get(k) for k in
                 ("creative", "storyboard", "copy") if data.get(k)}
    issues = check_fact_grounding(brief, artifacts, facts)
    return {"check": "fact_grounding", "passed": len(issues) == 0,
            "issue_count": len(issues),
            "issues": [i.model_dump() for i in issues[:5]],
            "detail": ("通过" if not issues
                       else f"{len(issues)} 处高风险宣称无事实支撑")}


def _recheck_risk(data: dict[str, Any]) -> dict[str, Any]:
    """风险重检：走 backend/risk.py rule_scan 确定性分级扫描。"""
    flags = _scan_risk_flags(data)
    high = [f for f in flags if f.get("level") == "高"]
    return {"check": "risk", "passed": len(high) == 0,
            "flag_count": len(flags),
            "high_risk_count": len(high),
            "flags": flags[:8],
            "detail": ("通过" if not high else f"{len(high)} 个高风险项")}


def _recheck_delivery(deps: ToolDeps, data: dict[str, Any]) -> dict[str, Any]:
    sb = data.get("storyboard") or {}
    r: ToolResult = run_tool(
        deps, "check_delivery",
        {"storyboard": sb if isinstance(sb, dict) else {},
         "check_type": "plan"})
    if r.status != "ok":
        return {"check": "delivery", "passed": False,
                "detail": f"交付检查执行失败: {r.error_message}",
                "error": r.error_code}
    delivery = r.data.get("delivery") or {}
    blockers = (delivery.get("blockers")
                or delivery.get("blocking_issues") or [])
    return {"check": "delivery", "passed": not blockers,
            "blockers": blockers[:5],
            "detail": "通过" if not blockers else f"{len(blockers)} 个阻断项"}


def _run_rechecks(deps: ToolDeps, data: dict[str, Any],
                  checks: list[str],
                  facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in checks:
        try:
            if name == "duration":
                results.append(_recheck_duration(data))
            elif name == "feasibility":
                results.append(_recheck_feasibility(data))
            elif name == "fact_grounding":
                results.append(_recheck_fact_grounding(data, facts))
            elif name == "risk":
                results.append(_recheck_risk(data))
            elif name == "delivery":
                results.append(_recheck_delivery(deps, data))
        except Exception as e:  # noqa: BLE001  单项重检失败不阻断其他项
            logger.warning("重检 %s 异常: %s", name, e)
            results.append({"check": name, "passed": False,
                            "detail": f"重检异常: {type(e).__name__}: {e}"})
    return results

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_identity(db: Session, user: Any, org_id: str) -> AgentIdentity:
    user_id = _normalize_user_id(user)
    ms = (db.query(Membership)
            .filter_by(user_id=user_id, org_id=org_id).first())
    if ms is None:
        raise EntityNotFound("Organization", org_id)
    return AgentIdentity(user_id=user_id, org_id=org_id, role=ms.role)


def _resolve_role(db: Session, user_id: str, org_id: str) -> str:
    ms = (db.query(Membership)
            .filter_by(user_id=user_id, org_id=org_id).first())
    return ms.role if ms else "viewer"


def _build_generate(task_id: str, org_id: str,
                    model: Optional[str],
                    ai_generate: Optional[Callable]
                    ) -> Callable[[str, Optional[str]], dict[str, Any]]:
    """构造 generate(prompt, feedback)->dict。

    ai_generate（测试 fake）优先；否则走网关真实调用（_ModelCaller，
    与 orchestrator 同口径：task_id 成本归属、429 硬停）。
    """
    if ai_generate is not None:
        def _fake(prompt: str,
                  feedback: Optional[str] = None) -> dict[str, Any]:
            try:
                return ai_generate(prompt, feedback)
            except TypeError:
                return ai_generate(prompt)
        return _fake
    from .orchestrator import _ModelCaller
    caller = _ModelCaller(task_id, org_id, model=model)
    return caller.generate


def _parse_context(db: Session, work: Any, data: dict[str, Any],
                   conv: Any, org_id: str) -> dict[str, Any]:
    facts = FactRepository(db).list_usable(org_id, limit=50)
    try:
        from .conversations import list_messages
        msgs = list_messages(db, conv.id, org_id, limit=6)
        recent = []
        for m in msgs:
            c = m.content_json or {}
            t = c.get("type") or m.role
            txt = c.get("text") or json.dumps(c, ensure_ascii=False)[:100]
            recent.append(f"{m.role}:{t}:{txt}")
    except Exception:  # noqa: BLE001  上下文组装失败不阻断
        recent = []
    return {
        "work_title": work.title,
        "artifacts": sorted(k for k in data.keys()
                            if k in PATCHABLE_DATA_KEYS),
        "storyboard_summary": _storyboard_summary(data),
        "locked_shots": _locked_shot_nos(db, work.id),
        "confirmed_facts": [f.content for f in facts],
        "recent_messages": recent,
    }


def _record_step(db: Session, task_id: str, tool_id: str,
                 params: dict[str, Any], status: str,
                 output_ref: Optional[str] = None,
                 error: Optional[str] = None,
                 duration_ms: int = 0) -> None:
    row = AgentStep(
        task_id=task_id, tool_id=tool_id,
        params_digest=step_params_digest(params),
        status=status, output_ref=output_ref, error=error,
        started_at=_utcnow(), finished_at=_utcnow(),
        duration_ms=duration_ms)
    db.add(row)
    db.commit()


def _risk_texts(data: dict[str, Any]) -> list[tuple[str, str]]:
    """收集待做风险扫描的文本：(位置, 文本)。"""
    texts: list[tuple[str, str]] = []
    sb = data.get("storyboard") or {}
    for sh in (sb.get("shots") or []):
        if not isinstance(sh, dict):
            continue
        no = sh.get("shot_no")
        texts.append((f"镜头{no}口播", str(sh.get("voiceover") or "")))
        texts.append((f"镜头{no}字幕", str(sh.get("subtitle") or "")))
    cp = data.get("copy") or {}
    if cp.get("text"):
        texts.append(("平台文案", str(cp["text"])))
    for d in (data.get("creative") or {}).get("directions", []):
        for sp in (d.get("selling_points") or []):
            texts.append(("创意卖点", str(sp)))
    return [(w, t) for w, t in texts if t.strip()]


def _scan_risk_flags(data: dict[str, Any]) -> list[dict[str, Any]]:
    """确定性风险扫描：走 backend/risk.py rule_scan（RISK_WORDS 分级）。

    这是仓库既有的分级口径（"高"=极限词/功效宣称/销量第一等）；
    check_risk 工具的确定性层只打"中"，不足以支撑"高风险变化"判定，
    故此处直接用 rule_scan。
    """
    flags: list[dict[str, Any]] = []
    for where, text in _risk_texts(data):
        flags.extend(rule_scan(text, where))
    return flags


def _risk_reason_on_new_data(new_data: dict[str, Any]) -> Optional[str]:
    """高风险变化判定：对生成后的新内容跑确定性风险层。

    高风险 → 返回确认原因；无高风险 → None。
    """
    high = [f for f in _scan_risk_flags(new_data)
            if f.get("level") == "高"]
    if high:
        kws = sorted({str(f.get("text")) for f in high})
        return (f"high_risk:修改后内容检出 {len(high)} 个高风险项"
                f"（{ '、'.join(kws[:5]) }），需人工确认")
    return None


def _summarize_change(plan: ModificationPlan,
                      impact: ModificationImpact,
                      data_patch: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, Any] = {
        "operation": plan.operation,
        "target_kind": plan.target_kind,
        "target": plan.target,
    }
    if plan.operation == "replace":
        changed["fields"] = sorted(
            list((plan.params.get("updates") or {}).keys())
            + ([plan.target] if "value" in plan.params else []))
    if impact.impacted_shots:
        changed["shots"] = impact.impacted_shots
    if plan.operation == "rewrite":
        changed["fields"] = sorted(
            list((plan.params.get("fields") or []))
            + list((plan.params.get("field_updates") or {}).keys()))
    changed["artifacts"] = impact.impacted_artifacts
    return changed


def _finish_run(db: Session, task_id: str, run_status: str,
                task_status: str, summary: dict[str, Any],
                error: Optional[str] = None) -> None:
    repo = AITaskRepository(db)
    payload = dict((repo.get(task_id).payload_json) or {})
    payload["run_status"] = run_status
    payload["summary"] = summary
    repo.update_payload(task_id, payload)
    if task_status == "succeeded":
        repo.set_status(task_id, "succeeded", progress=100,
                        result={"run_status": run_status,
                                "summary": summary})
    else:
        repo.set_status(task_id, task_status, error=error)


def _execute_pending(db: Session, task_id: str,
                     pending: dict[str, Any],
                     identity: AgentIdentity) -> dict[str, Any]:
    """执行已确认/自动的修改：快照 → patch → 重检 → 摘要 → 落终态。"""
    work_id = pending["work_id"]
    conv_id = pending.get("conversation_id")
    org_id = identity.org_id
    plan = ModificationPlan.model_validate(pending["plan"])
    impact = ModificationImpact.model_validate(pending["impact"])
    data_patch: dict[str, Any] = pending["data_patch"]
    base_version: int = pending["base_version"]
    t0 = time.time()

    work = WorkRepository(db).get(work_id, org_id)
    if work is None:
        raise ModifyExecutionError("执行时作品不存在（可能已被删除）")
    try:
        old_data = json.loads(work.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        raise ModifyExecutionError("作品 data_json 已损坏，无法做局部更新")
    if not isinstance(old_data, dict):
        raise ModifyExecutionError("作品 data_json 不是对象，无法做局部更新")

    # 1. 修改前快照（版本保护：一键恢复的基础）
    snap = _snapshot_work_version(db, work, identity.user_id)

    # 2. patch 语义执行：只写目标 artifact 键 + 乐观锁
    deps = ToolDeps(db=db, identity=identity, ai_generate=None)
    r: ToolResult = run_tool(
        deps, "patch_work",
        {"work_id": work_id, "expected_version": base_version,
         "data_patch": data_patch},
        confirmed=True)
    if r.status != "ok":
        _record_step(db, task_id, "modify", pending["plan"], "failed",
                     error=r.error_message or r.error_code,
                     duration_ms=int((time.time() - t0) * 1000))
        summary = {"status": "failed",
                   "error": f"写回失败（{r.error_code}）: {r.error_message}",
                   "snapshot_version_no": snap.version_no,
                   "restorable": True}
        _finish_run(db, task_id, RUN_STATUS_FAILED, "failed", summary,
                    error=summary["error"])
        if conv_id:
            append_message(db, conv_id, org_id, "assistant",
                           {"type": "modify_failed", "task_id": task_id,
                            "error": summary["error"]})
        return {"status": "failed", **summary, "task_id": task_id}

    new_version = r.data.get("version")
    work = WorkRepository(db).get(work_id, org_id)
    new_data = json.loads(work.data_json or "{}")
    _record_step(db, task_id, "modify", pending["plan"], "succeeded",
                 output_ref=f"work:{work_id}:v{new_version}",
                 duration_ms=int((time.time() - t0) * 1000))

    # 3. 改后重检（只跑受影响的检查项）
    facts = [{"id": f.id, "content": f.content, "status": f.status,
              "allow_use": f.allow_use}
             for f in FactRepository(db).list_usable(org_id, limit=200)]
    t1 = time.time()
    rechecks = _run_rechecks(deps, new_data, impact.checks_to_rerun, facts)
    _record_step(db, task_id, "modify_recheck",
                 {"checks": impact.checks_to_rerun},
                 "succeeded" if all(x.get("passed") for x in rechecks)
                 else "failed",
                 duration_ms=int((time.time() - t1) * 1000))

    # 4. 修改摘要
    summary = {
        "status": "completed",
        "task_id": task_id,
        "work_id": work_id,
        "work_version": new_version,
        "changed": _summarize_change(plan, impact, data_patch),
        "impacted_artifacts": impact.impacted_artifacts,
        "impacted_shots": impact.impacted_shots,
        "checks_rerun": impact.checks_to_rerun,
        "rechecks": rechecks,
        "rechecks_passed": all(x.get("passed") for x in rechecks),
        "snapshot_version_no": snap.version_no,
        "restorable": True,
        "requires_confirmation": False,
    }
    _finish_run(db, task_id, RUN_STATUS_COMPLETED, "succeeded", summary)
    if conv_id:
        append_message(db, conv_id, org_id, "assistant",
                       {"type": "modify_summary", "task_id": task_id,
                        "summary": {k: summary[k] for k in
                                    ("changed", "impacted_artifacts",
                                     "work_version", "rechecks_passed",
                                     "snapshot_version_no", "restorable")}})
    logger.info("修改执行完成 task=%s work=%s v%s 快照 v%s",
                task_id, work_id, new_version, snap.version_no)
    return summary


def apply_modification(
    db: Session,
    user: Any,
    org_id: str,
    work_id: str,
    conversation_id: str,
    user_text: str,
    *,
    model: Optional[str] = None,
    ai_generate: Optional[Callable] = None,
    max_repairs: int = MAX_MODIFY_REPAIRS,
) -> dict[str, Any]:
    """连续对话中的局部修改（任务书 §十）。

    返回：
    - {"status": "completed", ...修改摘要...}（自动执行）
    - {"status": "waiting_user", "task_id", "pending_step",
      "confirm_reason": ...}（需确认；调用 confirm_modification 继续）
    - {"status": "failed", "error": ...}

    跨作品拒绝：conversation.work_id 与 work_id 不一致 → EntityNotFound。
    """
    identity = _resolve_identity(db, user, org_id)

    # --- 跨作品拒绝（404/400，不执行任何写操作）---
    conv = get_conversation_or_404(db, conversation_id, org_id)
    if conv.work_id != work_id:
        raise EntityNotFound(
            "Work",
            f"跨作品修改被拒绝：会话绑定作品 {conv.work_id}，"
            f"请求目标作品 {work_id}")

    work = WorkRepository(db).get(work_id, org_id)
    if work is None:
        raise EntityNotFound("Work", work_id)
    try:
        data = json.loads(work.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        raise ModifyExecutionError("作品 data_json 已损坏，无法解析修改目标")
    if not isinstance(data, dict):
        raise ModifyExecutionError("作品 data_json 不是对象")

    # 建 run（挂在 agent_run 下，步骤/确认走统一机制）；复用会话串联多轮
    task_id = create_agent_run(
        db, user, org_id, work_id=work_id,
        brief_json={"modify_text": (user_text or "")[:500]},
        conversation_id=conversation_id)
    repo = AITaskRepository(db)
    repo.set_status(task_id, "running")
    append_message(db, conversation_id, org_id, "user",
                   {"type": "modify_request", "task_id": task_id,
                    "text": (user_text or "")[:1000]})

    generate = _build_generate(task_id, org_id, model, ai_generate)

    def _fail(reason: str) -> dict[str, Any]:
        summary = {"status": "failed", "task_id": task_id,
                   "work_id": work_id, "error": reason}
        _finish_run(db, task_id, RUN_STATUS_FAILED, "failed", summary,
                    error=reason)
        append_message(db, conversation_id, org_id, "assistant",
                       {"type": "modify_failed", "task_id": task_id,
                        "error": reason})
        return summary

    try:
        # 1. 意图解析
        ctx = _parse_context(db, work, data, conv, org_id)
        plan = parse_modification_from_text(
            user_text, generate, context=ctx, max_repairs=max_repairs)
        _record_step(db, task_id, "modify_parse",
                     {"text": (user_text or "")[:200]}, "succeeded")

        # 2. 影响分析
        impact = analyze_impact(db, plan, data, work_id)
        _record_step(db, task_id, "modify_impact",
                     {"plan": plan.model_dump()}, "succeeded")

        # 3. 保护规则（静态部分）
        reason = protection_reason(plan, impact)

        # 4. 需要模型时先生成内容（无论静态保护是否已命中；生成结果存入
        #    pending，确认后直接用，不重复烧 token；用户拒绝时多耗一次调用，
        #    换取确认路径无需模型、语义单一）
        generated: Optional[dict[str, Any]] = None
        if _needs_generation(plan):
            t0 = time.time()
            raw = generate(_make_generate_prompt(plan, data), None)
            generated = _validate_generated(plan, raw)
            _record_step(
                db, task_id, "modify_generate",
                {"target": plan.target}, "succeeded",
                duration_ms=int((time.time() - t0) * 1000))

        # 5. 计算 data_patch（只含目标 artifact 键）
        data_patch = _compute_data_patch(plan, data, generated)
        new_data = dict(data)
        new_data.update(data_patch)

        # 6. 高风险变化预检（只对会产生文本内容的修改）
        if reason is None and (
                _needs_generation(plan)
                or any(a in ("storyboard", "copy")
                       for a in impact.impacted_artifacts)):
            reason = _risk_reason_on_new_data(new_data)

        pending = {
            "plan": plan.model_dump(),
            "impact": impact.model_dump(),
            "generated": generated,
            "data_patch": data_patch,
            "work_id": work_id,
            "conversation_id": conversation_id,
            "base_version": work.version,
        }

        if reason is not None:
            # --- 先确认：挂起 waiting_user，确认后才执行 ---
            payload = dict((repo.get(task_id).payload_json) or {})
            payload["run_status"] = RUN_STATUS_WAITING_USER
            payload["pending_step"] = {
                "step_idx": 0, "tool_id": "modify",
                "params_digest": step_params_digest(pending["plan"]),
                "reason": reason,
                "requested_at": _utcnow().isoformat(),
            }
            payload["modify_pending"] = pending
            repo.update_payload(task_id, payload)
            _record_step(db, task_id, "modify", pending["plan"],
                         "waiting_user", error=reason)
            append_message(db, conversation_id, org_id, "assistant",
                           {"type": "modify_waiting_user",
                            "task_id": task_id,
                            "confirm_reason": reason,
                            "plan": plan.model_dump(),
                            "impact": impact.model_dump()})
            logger.info("修改挂起等待确认 task=%s 原因=%s", task_id, reason)
            return {"status": "waiting_user", "task_id": task_id,
                    "work_id": work_id,
                    "pending_step": payload["pending_step"],
                    "confirm_reason": reason,
                    "plan": plan.model_dump(),
                    "impact": impact.model_dump(),
                    "note": "调用 confirm_modification(task_id, 0, approved) "
                            "继续；approved=False 则取消"}

        # --- 自动执行 ---
        return _execute_pending(db, task_id, pending, identity)

    except RateLimitedStop as e:
        # 429 硬停：停服上报，不重试
        logger.error("修改流程撞 429 硬停 task=%s: %s", task_id, e)
        return _fail(f"rate_limited: {e}")
    except (ModifyParseError, ModifyExecutionError) as e:
        return _fail(str(e))
    except EntityNotFound:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("修改流程未预期异常 task=%s", task_id)
        return _fail(f"{type(e).__name__}: {e}")


def confirm_modification(db: Session, task_id: str, step_idx: int,
                         approved: bool,
                         org_id: Optional[str] = None) -> dict[str, Any]:
    """确认等待中的修改（复用 runner.confirm_step 的确认语义）。

    - approved=False → 任务 cancelled；
    - approved=True → 落确认态后直接执行待定修改（service 层同步语义；
      HTTP 层如需异步可自行入队），返回修改摘要。
    """
    st = confirm_step(db, task_id, step_idx, approved, org_id=org_id)
    if st.get("run_status") != RUN_STATUS_RUNNING:
        return {"status": st.get("run_status"), "task_id": task_id,
                "note": "用户拒绝，修改已取消"}
    repo = AITaskRepository(db)
    task = repo.get(task_id, org_id)
    payload = dict(task.payload_json or {})
    pending = payload.get("modify_pending")
    if not pending:
        raise ModifyExecutionError("该 run 没有待执行的修改计划")
    identity = AgentIdentity(user_id=task.created_by,
                             org_id=task.org_id,
                             role=_resolve_role(db, task.created_by,
                                                task.org_id))
    return _execute_pending(db, task_id, pending, identity)
