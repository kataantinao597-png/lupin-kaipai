"""Phase 2：结构化 JSON 计划生成与校验。

设计依据：PHASE1_DESIGN.md §1.1（供应商无 function calling，走
"JSON 计划 + 后端白名单分派"路线）、§4（任务执行模型）、§7（run 级上限）。

计划格式：{"steps": [{"tool_id": str, "params": {...}, "depends_on": [int]}]}
- 模型输出永远只是"提议"，执行权在后端：每步 tool_id 必须过白名单，
  params 必须过对应 Pydantic schema。
- run 级上限（任务书 §7.2）：最大步数 / 最大模型调用步数，超限拒绝整计划。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .tools import (
    TOOLS_REGISTRY,
    ToolTier,
    get_tool,
    is_tool_allowed,
    validate_tool_params,
    ToolParamError,
)

#: run 级上限（任务书 §7.2）
MAX_PLAN_STEPS = 25
MAX_MODEL_STEPS_PER_RUN = 8


@dataclass
class PlanStep:
    index: int
    tool_id: str
    params: dict[str, Any]
    depends_on: list[int] = field(default_factory=list)


@dataclass
class PlanValidation:
    ok: bool
    steps: list[PlanStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    needs_confirmation: list[int] = field(default_factory=list)
    model_steps: list[int] = field(default_factory=list)


def _has_cycle(n: int, edges: dict[int, list[int]]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n

    def dfs(u: int) -> bool:
        color[u] = GRAY
        for v in edges.get(u, []):
            if color[v] == GRAY:
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(dfs(i) for i in range(n) if color[i] == WHITE)


def validate_plan(raw: Any) -> PlanValidation:
    """校验结构化计划。返回 PlanValidation（ok=False 时 errors 非空）。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return PlanValidation(ok=False, errors=["plan 必须是 JSON 对象"])
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        return PlanValidation(ok=False, errors=["plan.steps 必须是非空数组"])
    if len(steps_raw) > MAX_PLAN_STEPS:
        return PlanValidation(
            ok=False,
            errors=[f"步数 {len(steps_raw)} 超过 run 上限 {MAX_PLAN_STEPS}"])

    steps: list[PlanStep] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            errors.append(f"steps[{i}] 必须是对象")
            continue
        tool_id = s.get("tool_id")
        if not isinstance(tool_id, str) or not is_tool_allowed(tool_id):
            errors.append(f"steps[{i}].tool_id 不在白名单: {tool_id!r}")
            continue
        params = s.get("params", {})
        if not isinstance(params, dict):
            errors.append(f"steps[{i}].params 必须是对象")
            continue
        try:
            validate_tool_params(tool_id, params)
        except ToolParamError as e:
            errors.append(f"steps[{i}] 参数非法: {e}")
            continue
        depends = s.get("depends_on", [])
        if not isinstance(depends, list) or any(
                not isinstance(d, int) or d < 0 or d >= len(steps_raw)
                for d in depends):
            errors.append(f"steps[{i}].depends_on 必须为有效的步骤索引数组")
            continue
        if i in depends:
            errors.append(f"steps[{i}] 不能依赖自身")
            continue
        steps.append(PlanStep(index=i, tool_id=tool_id,
                              params=params, depends_on=list(depends)))

    if errors:
        return PlanValidation(ok=False, errors=errors)

    if _has_cycle(len(steps), {s.index: s.depends_on for s in steps}):
        return PlanValidation(ok=False, errors=["steps 存在循环依赖"])

    needs_confirmation = [s.index for s in steps
                          if get_tool(s.tool_id).tier
                          == ToolTier.USER_CONFIRMATION_REQUIRED]
    model_steps = [s.index for s in steps
                   if get_tool(s.tool_id).needs_model]
    if len(model_steps) > MAX_MODEL_STEPS_PER_RUN:
        return PlanValidation(
            ok=False,
            errors=[f"模型调用步数 {len(model_steps)} 超过 run 上限 "
                    f"{MAX_MODEL_STEPS_PER_RUN}"])

    return PlanValidation(ok=True, steps=steps,
                          needs_confirmation=needs_confirmation,
                          model_steps=model_steps)


def summarize_plan(v: PlanValidation) -> dict[str, Any]:
    """给前端/日志的计划摘要（不含完整 params，避免 prompt 注入回显过大）。"""
    return {
        "ok": v.ok,
        "step_count": len(v.steps),
        "steps": [{"index": s.index, "tool_id": s.tool_id,
                   "depends_on": s.depends_on} for s in v.steps],
        "needs_confirmation": v.needs_confirmation,
        "model_steps": v.model_steps,
        "errors": v.errors,
    }
