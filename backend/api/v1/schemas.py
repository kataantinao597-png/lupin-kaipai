"""API v1 的 Pydantic 请求/响应模型。

031：严格 Schema——作品 data 自由 dict 已替换为 WorkData 等类型化模型。
WorkData 对未知字段保持 extra="allow"（作品文档随业务演进追加字段），
但已知字段必须类型正确（如 duration_seconds 必须为数字、facts 必须为数组），
非法结构会被 422 拦截，不再静默写入脏数据。
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...rbac import VALID_ROLES


class LenientModel(BaseModel):
    """业务文档类模型的基类：已知字段强类型，未知字段允许透传。"""

    model_config = ConfigDict(extra="allow")


# ---- 031：作品文档类型化 ----

class ShootSettings(LenientModel):
    device: str = "手机"
    people: int = 1
    lighting: str = ""
    location: str = ""
    target_duration_seconds: float = 60
    note: str = ""


class ProductInfo(LenientModel):
    product_name: str = ""
    description: str = ""
    selling_points: list[str] = Field(default_factory=list)
    price: str = ""
    audience: str = ""
    evidence_materials: str = ""
    shoot: ShootSettings = Field(default_factory=ShootSettings)


class FactItem(LenientModel):
    """产品事实库条目（与事实库表结构对齐的子集）。"""

    id: Optional[str] = None
    content: str = ""
    category: str = ""
    source_type: str = ""
    source: str = ""
    status: str = "pending"          # pending/confirmed/rejected/expired
    allow_use: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    version: int = 1


class ShotInfo(LenientModel):
    shot_number: int = 0
    scene: str = ""
    narration: str = ""
    subtitle: str = ""
    duration_seconds: float = 0
    locked: bool = False


class StoryboardInfo(LenientModel):
    title: str = ""
    shots: list[ShotInfo] = Field(default_factory=list)
    total_seconds: float = 0


class ShootShotState(LenientModel):
    status: str = "todo"             # todo/doing/done/retake
    note: str = ""
    updated_at: Optional[datetime] = None


class WorkData(LenientModel):
    """作品文档（替代原来的 dict[str, Any] 自由字段）。

    已知分区全部类型化；前端后续新增分区可直接透传（extra="allow"），
    后端读取时原样保留。
    """

    product: Optional[ProductInfo] = None
    facts: list[FactItem] = Field(default_factory=list)
    storyboard: Optional[StoryboardInfo] = None
    shoot_state: dict[str, ShootShotState] = Field(default_factory=dict)
    feasibility: Optional[dict[str, Any]] = None
    risk: Optional[dict[str, Any]] = None


# ---- 组织 ----
class OrgCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class OrgOut(BaseModel):
    id: str
    name: str
    created_by: str
    created_at: datetime
    my_role: str


class InviteCreate(BaseModel):
    role: str = Field(default="viewer", description="被邀请者加入后的默认角色")

    @field_validator("role")
    @classmethod
    def _role_valid(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"无效角色: {v}")
        return v


class InviteOut(BaseModel):
    code: str
    org_id: str
    role: str
    expires_at: datetime


class JoinRequest(BaseModel):
    code: str = Field(..., min_length=1)


class MemberOut(BaseModel):
    user_id: str
    role: str
    joined_at: datetime


class MemberRoleUpdate(BaseModel):
    role: str = Field(...)

    @field_validator("role")
    @classmethod
    def _role_valid(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"无效角色: {v}")
        return v


# ---- 作品 ----
class WorkCreate(BaseModel):
    org_id: str = Field(..., description="作品所属组织，创建时必须指定")
    title: str = Field(default="", max_length=256)
    data: WorkData = Field(default_factory=WorkData)  # 031：was dict[str, Any]


class WorkUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    data: Optional[WorkData] = None  # 031：was Optional[dict[str, Any]]，全量替换语义不变
    status: Optional[str] = Field(default=None, description="draft|active|archived")
    # 088：提供时做乐观锁检查，不匹配返回 409；不提供则保持兼容直接更新
    version: Optional[int] = Field(default=None, description="乐观锁版本号")


class WorkOut(BaseModel):
    id: str
    org_id: str
    owner_id: str
    title: str
    data: WorkData  # 031：was dict[str, Any]
    status: str = "draft"
    version: int = 1
    created_at: datetime
    updated_at: datetime
    # V3.0 B-5：作品引用的产品档案是否为旧版本（仅详情接口计算）
    product_ref_stale: bool = False


# ---- 作品历史版本（091） ----
class WorkVersionOut(BaseModel):
    id: str
    work_id: str
    version_no: int
    title: str
    created_by: str
    created_at: datetime


# ---- 回收站（092）：复用 WorkOut，加 deleted_at ----
class TrashWorkOut(WorkOut):
    deleted_at: Optional[datetime] = None


# ---- 031：导出接口类型化（was 裸 dict 返回） ----

class WorkExportWork(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    org_id: str
    owner_id: str
    title: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    data: WorkData


class WorkExportVersion(BaseModel):
    model_config = ConfigDict(extra="allow")

    version_no: int
    title: str
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    snapshot: WorkData


class WorkExportOut(BaseModel):
    format: str = "lupin-work-export"
    format_version: int = 1
    exported_at: datetime
    work: WorkExportWork
    versions: list[WorkExportVersion] = Field(default_factory=list)


class ExportAllOut(BaseModel):
    work: WorkOut
    exported_by_role: str


# ---- AI 异步任务（034/037/038，ai-tasks 组接口用；本组仅补齐定义以保证导入） ----
# TODO(027)：ai-tasks 组接口确认是否接入标准响应信封（ApiResponse），由该组决定。

class AITaskCreate(BaseModel):
    task_type: str = Field(..., description="任务类型，见 models.TASK_TYPES")
    org_id: Optional[str] = Field(default=None, description="组织 ID，不传则用内置 default 组织")
    payload: Optional[dict[str, Any]] = Field(default=None, description="任务输入（不得含 API Key）")
    created_by: Optional[str] = None
    priority: str = Field(default="high", description="high=用户交互优先，low=后台批量")


class AITaskOut(BaseModel):
    id: str
    org_id: str
    task_type: str
    status: str
    progress: int = 0
    result: Optional[Any] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    priority: str = "high"
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class AITaskCancelOut(BaseModel):
    id: str
    status: str
    cancelled: bool
    message: str = ""


class AITaskListOut(BaseModel):
    items: list[AITaskOut] = Field(default_factory=list)
    total: int = 0


# ---- S6.1：事实库（状态机 + 审计 + 版本） ----

class FactCreate(BaseModel):
    org_id: str = Field(..., description="组织 ID")
    content: str = Field(..., min_length=1, description="事实内容")
    product_id: str = Field(default="", description="关联产品 ID")
    category: str = Field(default="", description="事实分类")
    source_type: str = Field(
        default="human_statement",
        description="来源类型：official_document|enterprise_material|human_statement|ai_inference")
    source_ref: str = Field(default="", description="来源引用/证据链接")
    scope: str = Field(default="", description="适用范围，如：仅烟台产地款")
    expires_at: Optional[datetime] = Field(default=None, description="有效期截止")

    @field_validator("source_type")
    @classmethod
    def _check_source_type(cls, v: str) -> str:
        allowed = {"official_document", "enterprise_material",
                   "human_statement", "ai_inference"}
        if v not in allowed:
            raise ValueError(f"source_type 非法，应为 {sorted(allowed)} 之一")
        return v


class FactOut(BaseModel):
    id: str
    org_id: str
    product_id: str = ""
    content: str
    category: str = ""
    source_type: str = ""
    source_ref: str = ""
    scope: str = ""
    status: str
    allow_use: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FactTransitionReq(BaseModel):
    org_id: str = Field(..., description="组织 ID")
    target_status: str = Field(
        ..., description="目标状态：pending_review|confirmed|rejected|expired|revoked|draft")
    reason: str = Field(default="", description="流转原因（确认/驳回/撤销必填）")
    evidence_ref: str = Field(default="", description="证据引用")
    expected_version: Optional[int] = Field(
        default=None, description="乐观锁版本号，不传则跳过校验")
    allow_use: Optional[bool] = Field(
        default=None, description="确认时可显式设置 allow_use（默认 true）")


class FactHistoryOut(BaseModel):
    id: str
    fact_id: str
    from_status: str
    to_status: str
    operator_id: str
    reason: str = ""
    evidence_ref: str = ""
    created_at: Optional[datetime] = None


class FactContentUpdate(BaseModel):
    org_id: str = Field(..., description="组织 ID")
    expected_version: int = Field(..., description="乐观锁版本号（必填）")
    content: Optional[str] = Field(default=None, description="事实内容")
    category: Optional[str] = Field(default=None, description="事实分类")
    source_type: Optional[str] = Field(default=None, description="来源类型")
    source_ref: Optional[str] = Field(default=None, description="来源引用")


class FactVersionOut(BaseModel):
    id: str
    fact_id: str
    version_no: int
    content: str = ""
    category: str = ""
    source_type: str = ""
    source_ref: str = ""
    status: str = ""
    allow_use: bool = False
    created_by: str = ""
    created_at: Optional[datetime] = None


# ---------------- V3.0 B-1：产品档案 ----------------

class ProductCreate(BaseModel):
    org_id: str = Field(..., description="组织 ID")
    code: str = Field(..., min_length=1, description="产品编号（组织内唯一，创建后不可改）")
    name: str = Field(..., min_length=1, description="产品名称")
    category: str = Field(default="", description="品类")
    origin: str = Field(default="", description="产地")
    brand_profile: str = Field(default="", description="品牌资料")
    brand_tone: str = Field(default="", description="品牌语气")
    selling_points: list[str] = Field(default_factory=list, description="产品卖点")
    ad_restrictions: str = Field(default="", description="宣传限制")
    no_expand_claims: str = Field(default="", description="禁止擅自扩展的信息")


class ProductUpdate(BaseModel):
    org_id: str = Field(..., description="组织 ID")
    expected_version: int = Field(..., description="乐观锁版本号（必填）")
    name: Optional[str] = Field(default=None, description="产品名称")
    category: Optional[str] = Field(default=None, description="品类")
    origin: Optional[str] = Field(default=None, description="产地")
    brand_profile: Optional[str] = Field(default=None, description="品牌资料")
    brand_tone: Optional[str] = Field(default=None, description="品牌语气")
    selling_points: Optional[list[str]] = Field(default=None, description="产品卖点")
    ad_restrictions: Optional[str] = Field(default=None, description="宣传限制")
    no_expand_claims: Optional[str] = Field(default=None, description="禁止擅自扩展的信息")


class ProductOut(BaseModel):
    id: str
    org_id: str
    code: str
    name: str
    category: str = ""
    origin: str = ""
    brand_profile: str = ""
    brand_tone: str = ""
    selling_points: list[str] = []
    ad_restrictions: str = ""
    no_expand_claims: str = ""
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # V3.0 B-5：本次 PATCH 被标记为旧版本引用的条数（仅 PATCH 响应有意义）
    stale_refs_marked: int = 0
