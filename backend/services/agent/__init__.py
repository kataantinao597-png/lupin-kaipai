"""Phase 2：Agent 工具层（backend/services/agent/）。

- tools：工具定义 / 参数 schema / 注册表 / 白名单 / 工具分级
- context：上下文装配 + check_user_quota() 桩位
- adapters：工具适配器（只调既有 service/repository）+ 统一分派 run_tool
- planner：结构化 JSON 计划生成与校验（白名单分派路线）
"""
# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入——models.py:35 的
# `from .auth.models import Base` 会触发 backend/auth/__init__ → deps →
# repositories → 回到部分初始化的 models，ImportError。
# 安全顺序是先走 backend.auth.*（app.py 与 tests 均如此），这里显式固定。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

from .adapters import ToolDeps, ToolResult, run_tool
from .context import AgentIdentity, assemble_agent_context, check_user_quota
from .planner import (
    MAX_MODEL_STEPS_PER_RUN,
    MAX_PLAN_STEPS,
    PlanStep,
    PlanValidation,
    summarize_plan,
    validate_plan,
)
from .tools import (
    PATCHABLE_DATA_KEYS,
    TOOLS_REGISTRY,
    ToolDefinition,
    ToolParamError,
    ToolTier,
    get_tool,
    is_tool_allowed,
    tools_needing_model,
    tools_requiring_confirmation,
    validate_tool_params,
)

__all__ = [
    "AgentIdentity",
    "MAX_MODEL_STEPS_PER_RUN",
    "MAX_PLAN_STEPS",
    "PATCHABLE_DATA_KEYS",
    "TOOLS_REGISTRY",
    "PlanStep",
    "PlanValidation",
    "ToolDefinition",
    "ToolDeps",
    "ToolParamError",
    "ToolResult",
    "ToolTier",
    "assemble_agent_context",
    "check_user_quota",
    "get_tool",
    "is_tool_allowed",
    "run_tool",
    "summarize_plan",
    "tools_needing_model",
    "tools_requiring_confirmation",
    "validate_plan",
    "validate_tool_params",
]
