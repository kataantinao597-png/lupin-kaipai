"""Phase 2：Agent 工具注册表 / 参数 schema / 白名单 / 工具分级。

设计依据：docs/agent-upgrade/PHASE1_DESIGN.md §1.3（缺失 1、5）、§3、§5；
映射依据：docs/agent-upgrade/TOOL_MAPPING.md（8 逻辑工具 + 6 扩展工具）。

约束：
- 参数 schema 全部是 Pydantic v2 模型；模型输出/用户输入视为不可信，
  schema 只做长度/枚举/范围限制，禁止出现 shell/SQL/文件路径/URL 语义的字段。
- 白名单 = TOOLS_REGISTRY 的键集合；planner/dispatcher 只允许分派已注册 tool_id。
- 分级：core_required（只读/确定性检查，可直接执行） /
  optional（调用模型或高成本，可编排执行） /
  user_confirmation_required（破坏性/不可逆，必须先落 waiting_user 等用户确认）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolTier(str, Enum):
    CORE_REQUIRED = "core_required"
    OPTIONAL = "optional"
    USER_CONFIRMATION_REQUIRED = "user_confirmation_required"


# ---------------------------------------------------------------------------
# 参数 schema（全部 Pydantic v2；extra=forbid 拒绝未知字段，防止模型塞私货）
# ---------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QueryFactsParams(_Strict):
    """产品事实查询 → FactRepository.list_usable（已确认+允许使用+未过期）。"""
    product_id: Optional[str] = Field(default=None, max_length=64)
    status: Literal["usable", "all", "confirmed", "pending"] = Field(
        default="usable",
        description="usable=仅 AI 可引用的已确认事实；all=按状态过滤的全部",
    )
    limit: int = Field(default=50, ge=1, le=200)
    fact_id: Optional[str] = Field(default=None, max_length=64,
                                   description="指定事实 id 时只查单条")


class QueryProductsParams(_Strict):
    """产品档案查询 → ProductRepository。"""
    product_id: Optional[str] = Field(default=None, max_length=64)
    code: Optional[str] = Field(default=None, max_length=64,
                                description="产品编号精确匹配")
    keyword: Optional[str] = Field(default=None, max_length=64,
                                   description="名称模糊匹配")
    limit: int = Field(default=50, ge=1, le=200)


class GenerateIdeasParams(_Strict):
    """营销策划/创意 → 需模型；adapter 通过注入的 ai 服务调用。"""
    brief: str = Field(min_length=1, max_length=4000)
    work_id: Optional[str] = Field(default=None, max_length=64)
    product_id: Optional[str] = Field(default=None, max_length=64)
    idea_count: int = Field(default=3, ge=1, le=10)
    inject_confirmed_facts: bool = Field(
        default=True, description="是否把已确认事实注入 prompt（保真）")


class GenerateStoryboardParams(_Strict):
    """分镜生成 → 需模型。"""
    work_id: Optional[str] = Field(default=None, max_length=64)
    brief: str = Field(min_length=1, max_length=4000)
    idea: str = Field(default="", max_length=4000,
                      description="选定的创意方向（可空）")
    shot_count: int = Field(default=5, ge=1, le=20)
    duration_sec: int = Field(default=60, ge=5, le=600,
                             description="成片目标时长（秒）")


class CheckRiskParams(_Strict):
    """风险/事实 grounding 检查 → 确定性规则层必跑；AI 层可选（需注入 ai）。"""
    storyboard: dict = Field(description="分镜 JSON（含 shots 数组）")
    work_id: Optional[str] = Field(default=None, max_length=64)
    selling_points: list[str] = Field(default_factory=list, max_length=50)
    evidence_materials: str = Field(default="", max_length=4000,
                                    description="用户提供的依据材料原文")
    run_ai_layer: bool = Field(default=False,
                               description="是否同时跑 AI 识别层（耗模型调用）")


class ShootingChecklistParams(_Strict):
    """实拍指导/拍摄清单 → 确定性：validator 校验 + 镜头清单组装。"""
    storyboard: dict = Field(description="分镜 JSON（含 shots 数组）")
    shoot_state: dict = Field(default_factory=dict,
                              description="拍摄进度状态（如已拍镜头）")
    work_id: Optional[str] = Field(default=None, max_length=64)


class RewriteCopyParams(_Strict):
    """文案改写（拉呱风 colloquial_rewrite）→ 需模型。"""
    text: str = Field(min_length=1, max_length=2000,
                      description="待改写的书面口播文案")
    product_name: str = Field(default="", max_length=100)
    work_id: Optional[str] = Field(default=None, max_length=64)
    product_id: Optional[str] = Field(default=None, max_length=64)
    style: Literal["colloquial", "plain"] = Field(
        default="colloquial", description="colloquial=山东农人口语味")


class CheckDeliveryParams(_Strict):
    """交付检查 → delivery_service.check_delivery（确定性）。"""
    work_id: Optional[str] = Field(default=None, max_length=64)
    storyboard: Optional[dict] = Field(default=None,
                                       description="分镜 JSON（可空，用作品数据）")
    check_type: Literal["plan", "final"] = Field(default="plan")


class ParseBriefParams(_Strict):
    """对话理解/brief 解析 → 需模型，结构化抽取任务书 §5.1 字段。"""
    user_text: str = Field(min_length=1, max_length=4000,
                           description="用户一句话")
    work_id: Optional[str] = Field(default=None, max_length=64)
    product_id: Optional[str] = Field(default=None, max_length=64)


class PatchWorkParams(_Strict):
    """作品局部更新 → patch 语义（只写目标字段 + 乐观锁），破坏性，需确认。

    至少给 title 或 data_patch 之一；data_patch 的键限定在允许的白名单字段，
    防止模型重写无关内容（任务书硬性指标）。
    """
    work_id: str = Field(max_length=64)
    expected_version: int = Field(ge=1, description="乐观锁版本号，必填")
    title: Optional[str] = Field(default=None, max_length=256)
    data_patch: Optional[dict] = Field(
        default=None,
        description="data_json 的局部补丁；键仅允许 PATCHABLE_DATA_KEYS",
    )
    status: Optional[Literal["draft", "active", "archived"]] = Field(default=None)


#: patch_work 允许改写的 data_json 顶层键白名单
PATCHABLE_DATA_KEYS = frozenset({
    "brief", "idea", "storyboard", "voiceover", "subtitle",
    "shoot_notes", "cover", "tags",
})


class GetTaskStatusParams(_Strict):
    """任务状态查询 → AITask 只读（org 隔离）。"""
    task_id: str = Field(max_length=64)


class QueryUsageParams(_Strict):
    """成本/用量/预算查询 → ai_ledger.org_calls_query 聚合。"""
    task_id: Optional[str] = Field(default=None, max_length=64,
                                   description="按 run（agent_run 的 AITask.id）过滤")
    limit: int = Field(default=100, ge=1, le=500,
                       description="最近 N 条调用明细")


class RegenerateShotParams(_Strict):
    """单镜头局部重生成 → 需模型；adapter 强制检查 Shot.locked。"""
    work_id: str = Field(max_length=64)
    storyboard_id: str = Field(max_length=64)
    shot_no: int = Field(ge=1, le=100)
    instruction: str = Field(min_length=1, max_length=1000,
                             description="重生成指令（用户原话/改写要求）")


class DeleteWorkParams(_Strict):
    """作品删除（软删除）→ 破坏性，需确认。"""
    work_id: str = Field(max_length=64)


# ---------------------------------------------------------------------------
# 工具定义 / 注册表 / 白名单
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    tier: ToolTier
    description: str
    params_model: type[BaseModel]
    rbac_action: str
    needs_model: bool = False
    writes_artifact: bool = False
    source: str = ""  # 复用的既有实现位置（TOOL_MAPPING 证据）


TOOLS_REGISTRY: dict[str, ToolDefinition] = {}


def _register(defn: ToolDefinition) -> None:
    if defn.tool_id in TOOLS_REGISTRY:
        raise ValueError(f"tool_id 重复注册: {defn.tool_id}")
    TOOLS_REGISTRY[defn.tool_id] = defn


# ---- 8 逻辑工具（任务书 §1.2）----
_register(ToolDefinition(
    tool_id="query_facts", tier=ToolTier.CORE_REQUIRED,
    description="查询组织事实库（默认仅已确认、可使用、未过期）",
    params_model=QueryFactsParams, rbac_action="fact:read",
    source="backend/repositories/fact_repository.py: FactRepository.list_usable/get",
))
_register(ToolDefinition(
    tool_id="query_products", tier=ToolTier.CORE_REQUIRED,
    description="查询产品档案（id/编号/关键词）",
    params_model=QueryProductsParams, rbac_action="product:read",
    source="backend/repositories/product_repository.py: ProductRepository.list_by_org/get/get_by_code",
))
_register(ToolDefinition(
    tool_id="generate_ideas", tier=ToolTier.OPTIONAL,
    description="营销策划/创意生成（需模型调用；结果不落盘，由 patch_work 写回）",
    params_model=GenerateIdeasParams, rbac_action="work:read",
    needs_model=True,
    source="backend/api/v1/ideas.py:223 POST /api/ideas（_validate_ideas 123 行）",
))
_register(ToolDefinition(
    tool_id="generate_storyboard", tier=ToolTier.OPTIONAL,
    description="分镜生成（需模型调用；结果不落盘，由 patch_work 写回）",
    params_model=GenerateStoryboardParams, rbac_action="work:read",
    needs_model=True,
    source="backend/api/v1/ideas.py:290 POST /api/storyboard（_validate_shot 141 行）",
))
_register(ToolDefinition(
    tool_id="check_risk", tier=ToolTier.CORE_REQUIRED,
    description="风险检查：确定性事实 grounding 规则层必跑；AI 识别层可选",
    params_model=CheckRiskParams, rbac_action="work:read",
    source="backend/services/risk_service.py:17 check_storyboard_risk + backend/risk.py check_fact_grounding",
))
_register(ToolDefinition(
    tool_id="shooting_checklist", tier=ToolTier.OPTIONAL,
    description="实拍指导：分镜校验 + 拍摄/补拍清单组装（确定性，不耗模型）",
    params_model=ShootingChecklistParams, rbac_action="work:read",
    source="backend/api/v1/exports.py:58 POST /api/shooting-checklist（ChecklistRequest 31 行）",
))
_register(ToolDefinition(
    tool_id="rewrite_copy", tier=ToolTier.OPTIONAL,
    description="文案改写：书面口播 → 山东农人口语味（需模型调用）",
    params_model=RewriteCopyParams, rbac_action="copy:create",
    needs_model=True,
    source="backend/api/v1/colloquial.py:55 POST /api/colloquial-rewrite（_validate_colloquial 39 行）",
))
_register(ToolDefinition(
    tool_id="check_delivery", tier=ToolTier.CORE_REQUIRED,
    description="交付检查：能不能交付的确定性判定（阻断项/建议项）",
    params_model=CheckDeliveryParams, rbac_action="work:read",
    source="backend/services/delivery_service.py:19 check_delivery",
))

# ---- 6 扩展工具 ----
_register(ToolDefinition(
    tool_id="parse_brief", tier=ToolTier.CORE_REQUIRED,
    description="对话理解：用户一句话 → 结构化 brief（需模型调用）",
    params_model=ParseBriefParams, rbac_action="org:read",
    needs_model=True,
    source="缺失项 1（Phase 1 审计）；走 gateway.generate_with_retry 结构化抽取",
))
_register(ToolDefinition(
    tool_id="patch_work", tier=ToolTier.USER_CONFIRMATION_REQUIRED,
    description="作品局部更新：只写目标字段 + 乐观锁版本校验（破坏性，需用户确认）",
    params_model=PatchWorkParams, rbac_action="work:update",
    writes_artifact=True,
    source="backend/repositories/work_repository.py:68 update（088 乐观锁）",
))
_register(ToolDefinition(
    tool_id="get_task_status", tier=ToolTier.CORE_REQUIRED,
    description="查询 AI 异步任务状态（含 agent_run；org 隔离）",
    params_model=GetTaskStatusParams, rbac_action="org:read",
    source="backend/api/v1/ai_tasks.py:155 GET /{task_id}（_require_org_member）",
))
_register(ToolDefinition(
    tool_id="query_usage", tier=ToolTier.CORE_REQUIRED,
    description="成本/用量查询：按组织聚合 ai_calls，可按 run（task_id）过滤",
    params_model=QueryUsageParams, rbac_action="org:read",
    source="backend/services/ai_ledger.py:33 org_calls_query",
))
_register(ToolDefinition(
    tool_id="regenerate_shot", tier=ToolTier.OPTIONAL,
    description="单镜头局部重生成：强制检查 Shot.locked，锁定镜头拒绝（需模型调用）",
    params_model=RegenerateShotParams, rbac_action="work:read",
    needs_model=True,
    source="backend/api/v1/ideas.py:367 POST /api/regenerate-shot；backend/repositories/storyboard_repository.py:192 set_shot_lock",
))
_register(ToolDefinition(
    tool_id="delete_work", tier=ToolTier.USER_CONFIRMATION_REQUIRED,
    description="作品删除（软删除；破坏性，需用户确认）",
    params_model=DeleteWorkParams, rbac_action="work:delete",
    writes_artifact=True,
    source="backend/api/v1/works.py:354 DELETE（软删除）；work_repository.py:115 soft_delete",
))


# ---------------------------------------------------------------------------
# 白名单校验 / 参数校验
# ---------------------------------------------------------------------------

class ToolParamError(ValueError):
    """参数 schema 校验失败 → adapter 转 error_code=invalid_params。"""

    def __init__(self, tool_id: str, errors: list[dict]):
        self.tool_id = tool_id
        self.errors = errors
        msgs = "; ".join(
            f"{'.'.join(map(str, e.get('loc', ())))}: {e.get('msg')}"
            for e in errors
        )
        super().__init__(f"工具 {tool_id} 参数非法: {msgs}")


def is_tool_allowed(tool_id: str) -> bool:
    """白名单校验：只有注册表里的 tool_id 可被分派。"""
    return tool_id in TOOLS_REGISTRY


def get_tool(tool_id: str) -> Optional[ToolDefinition]:
    return TOOLS_REGISTRY.get(tool_id)


def validate_tool_params(tool_id: str, params: dict[str, Any]) -> BaseModel:
    """校验工具参数；tool_id 不在白名单 → KeyError；参数非法 → ToolParamError。"""
    defn = TOOLS_REGISTRY.get(tool_id)
    if defn is None:
        raise KeyError(f"tool_id 不在白名单: {tool_id}")
    if not isinstance(params, dict):
        raise ToolParamError(tool_id, [{"loc": ("params",), "msg": "params 必须是对象"}])
    try:
        return defn.params_model.model_validate(params)
    except ValidationError as e:
        raise ToolParamError(tool_id, e.errors())


def tools_requiring_confirmation() -> list[str]:
    return [t.tool_id for t in TOOLS_REGISTRY.values()
            if t.tier == ToolTier.USER_CONFIRMATION_REQUIRED]


def tools_needing_model() -> list[str]:
    return [t.tool_id for t in TOOLS_REGISTRY.values() if t.needs_model]
