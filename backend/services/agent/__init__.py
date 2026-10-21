"""Phase 2：Agent 工具层（backend/services/agent/）。

- tools：工具定义 / 参数 schema / 注册表 / 白名单 / 工具分级
- context：上下文装配 + check_user_quota() 桩位
- adapters：工具适配器（只调既有 service/repository）+ 统一分派 run_tool
- planner：结构化 JSON 计划生成与校验（白名单分派路线）
- conversations（Phase 3）：对话持久化 ORM + CRUD（org 归属校验）
- runner（Phase 3）：agent_run 生命周期（AITask task_type="agent_run"）
- brief（Phase 5）：自然语言 → 结构化 Brief（§5.1 schema + 透明默认 + 待核实）
- orchestrator（Phase 5）：一句话生成完整项目（§5.2 11 步编排）
- quality_gates（Phase 5）：artifact schema / 一致性 / 风险引用 / 持久化门禁
- modify（Phase 6）：连续对话局部修改（意图解析/影响分析/保护规则/
  版本快照与一键恢复/patch 语义执行/改后重检/修改摘要）
"""
# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入——models.py:35 的
# `from .auth.models import Base` 会触发 backend/auth/__init__ → deps →
# repositories → 回到部分初始化的 models，ImportError。
# 安全顺序是先走 backend.auth.*（app.py 与 tests 均如此），这里显式固定。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

from .adapters import ToolDeps, ToolResult, run_tool
from .brief import (
    DEFAULTS,
    MAX_BRIEF_REPAIRS,
    Brief,
    BriefParseError,
    brief_to_prompt_text,
    merge_brief,
    merge_brief_dict,
    parse_brief_from_text,
)
from .context import AgentIdentity, assemble_agent_context, check_user_quota
from .conversations import (
    MESSAGE_ROLES,
    AgentStep,
    Conversation,
    ConversationMessage,
    append_message,
    count_messages,
    create_conversation,
    get_conversation,
    get_conversation_or_404,
    list_conversations,
    list_messages,
    set_conversation_status,
)
from .planner import (
    MAX_MODEL_STEPS_PER_RUN,
    MAX_PLAN_STEPS,
    PlanStep,
    PlanValidation,
    summarize_plan,
    validate_plan,
)
from .modify import (
    MAX_AUTO_ARTIFACTS,
    MAX_MODIFY_REPAIRS,
    MAX_VERSIONS_PER_WORK,
    ModificationImpact,
    ModificationPlan,
    ModifyExecutionError,
    ModifyParseError,
    analyze_impact,
    apply_modification,
    confirm_modification,
    parse_modification_from_text,
    protection_reason,
    restore_modification,
)
from .orchestrator import (
    ModelStepsExceeded,
    RateLimitedStop,
    run_one_shot_brief,
)
from .quality_gates import (
    HIGH_RISK_KEYWORDS,
    CopyArtifact,
    CreativeArtifact,
    GateIssue,
    GateResult,
    QualityReport,
    ShootingListArtifact,
    StoryboardArtifact,
    StrategyArtifact,
    check_consistency,
    check_fact_grounding,
    run_quality_gates,
    validate_artifact,
    verify_persistence,
)
from .runner import (
    RUN_STATUSES,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_USER,
    TASK_TYPE_AGENT_RUN,
    confirm_step,
    create_agent_run,
    execute_run_steps,
    get_run_state,
    run_agent_run,
    set_run_plan,
    step_params_digest,
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
    "AgentStep",
    "Brief",
    "BriefParseError",
    "Conversation",
    "ConversationMessage",
    "CopyArtifact",
    "CreativeArtifact",
    "DEFAULTS",
    "GateIssue",
    "GateResult",
    "HIGH_RISK_KEYWORDS",
    "MAX_AUTO_ARTIFACTS",
    "MAX_BRIEF_REPAIRS",
    "MAX_MODEL_STEPS_PER_RUN",
    "MAX_MODIFY_REPAIRS",
    "MAX_PLAN_STEPS",
    "MAX_VERSIONS_PER_WORK",
    "MESSAGE_ROLES",
    "ModificationImpact",
    "ModificationPlan",
    "ModelStepsExceeded",
    "ModifyExecutionError",
    "ModifyParseError",
    "PATCHABLE_DATA_KEYS",
    "QualityReport",
    "RUN_STATUSES",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_PARTIAL",
    "RUN_STATUS_QUEUED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_WAITING_USER",
    "RateLimitedStop",
    "ShootingListArtifact",
    "StoryboardArtifact",
    "StrategyArtifact",
    "TASK_TYPE_AGENT_RUN",
    "TOOLS_REGISTRY",
    "PlanStep",
    "PlanValidation",
    "ToolDefinition",
    "ToolDeps",
    "ToolParamError",
    "ToolResult",
    "ToolTier",
    "analyze_impact",
    "append_message",
    "apply_modification",
    "assemble_agent_context",
    "brief_to_prompt_text",
    "check_consistency",
    "check_fact_grounding",
    "check_user_quota",
    "confirm_modification",
    "confirm_step",
    "count_messages",
    "create_agent_run",
    "create_conversation",
    "execute_run_steps",
    "get_conversation",
    "get_conversation_or_404",
    "get_run_state",
    "get_tool",
    "is_tool_allowed",
    "list_conversations",
    "list_messages",
    "merge_brief",
    "merge_brief_dict",
    "parse_brief_from_text",
    "parse_modification_from_text",
    "protection_reason",
    "restore_modification",
    "run_agent_run",
    "run_one_shot_brief",
    "run_quality_gates",
    "run_tool",
    "set_conversation_status",
    "set_run_plan",
    "step_params_digest",
    "summarize_plan",
    "tools_needing_model",
    "tools_requiring_confirmation",
    "validate_artifact",
    "validate_plan",
    "validate_tool_params",
    "verify_persistence",
]
