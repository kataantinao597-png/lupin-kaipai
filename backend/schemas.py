"""鲁品开拍 - Pydantic 数据模型（对应实验 schema.json 的字段与枚举）。"""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

SHOT_TYPES = ["特写", "近景", "中景", "中远景", "全景", "远景"]


class ShootInfo(BaseModel):
    device: str = Field(..., min_length=1)
    people: int = Field(..., ge=1)
    lighting: Optional[str] = ""
    location: Optional[str] = ""


class Shot(BaseModel):
    shot_number: int = Field(..., ge=1)
    duration_seconds: float = Field(..., gt=0)
    shot_type: Literal["特写", "近景", "中景", "中远景", "全景", "远景"]
    scene_description: str = Field(..., min_length=1)
    shooting_action: str = Field(..., min_length=1)
    voiceover: str = Field(..., min_length=1)
    subtitle: str = Field(..., min_length=1)
    props: List[str] = Field(default_factory=list)
    required_assets: List[str] = Field(default_factory=list)
    camera_movement: str = Field(..., min_length=1)
    evidence_notes: str = Field(default="无")
    fact_refs: List[str] = Field(default_factory=list, description="引用的事实ID")


class RiskFlag(BaseModel):
    text: str = Field(..., min_length=1)
    level: Literal["高", "中", "低"]
    reason: str = Field(default="")
    confirmed: bool = Field(default=False, description="人工是否已确认")


class Storyboard(BaseModel):
    product_summary: str = Field(..., min_length=1)
    target_audience: str = Field(..., min_length=1)
    creative_direction: str = Field(..., min_length=1)
    target_duration_seconds: float = Field(..., gt=0)
    shoot: ShootInfo
    shots: List[Shot] = Field(..., min_length=1)
    materials_checklist: List[str] = Field(default_factory=list)
    risk_flags: List[RiskFlag] = Field(default_factory=list)
    pending_confirmations: List[str] = Field(default_factory=list)


FACT_CATEGORIES = ["产地", "品种", "口感", "工艺", "规格", "保存方式", "认证", "其他"]


class ProductFact(BaseModel):
    """产品事实库条目。"""
    id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, description="事实内容")
    category: str = Field(default="其他", description="分类")
    source: str = Field(default="", description="信息来源/证明材料")
    status: Literal["confirmed", "pending"] = Field(default="pending", description="已确认/待核实")
    allow_use: bool = Field(default=True, description="是否允许用于内容生成")


class Idea(BaseModel):
    name: str = Field(..., min_length=1)
    tagline: str = Field(default="")
    hook_3s: str = Field(default="")
    core_idea: str = Field(default="")
    audience_fit: str = Field(default="")
    difficulty: str = Field(default="")
    materials_needed: str = Field(default="")
    fact_refs: List[str] = Field(default_factory=list, description="引用的事实ID")


# ---------- 拍摄可行性检查 ----------

FeasibilityStatus = Literal["可执行", "建议调整", "不可执行", "需要人工确认"]


class ShotAlternative(BaseModel):
    """单个镜头的可执行替代方案。"""
    shooting_method: str = Field(..., min_length=1, description="拍摄方式")
    device_usage: str = Field(..., min_length=1, description="现有设备的具体使用方法")
    camera_position: str = Field(..., min_length=1, description="机位/拍摄角度")
    action: str = Field(..., min_length=1, description="人物及产品动作")
    props_needed: List[str] = Field(default_factory=list, description="必要的道具/场地要求")
    suggested_duration: float = Field(..., gt=0, description="建议时长（秒）")
    preserved_purpose: str = Field(..., min_length=1, description="保留的原始表达目的")


class ShotFeasibility(BaseModel):
    """单个镜头的可行性检查结果。"""
    shot_number: int = Field(..., ge=1)
    status: FeasibilityStatus
    reason: str = Field(..., min_length=1, description="具体判断原因")
    constraints_involved: List[str] = Field(default_factory=list, description="涉及的拍摄约束")
    source: Literal["规则", "模型"] = Field(default="模型", description="判断来源")
    alternative: Optional[ShotAlternative] = Field(default=None, description="替代方案（仅建议调整/不可执行时提供）")


class FeasibilityReport(BaseModel):
    """整组镜头的可行性检查报告。"""
    results: List[ShotFeasibility] = Field(..., min_length=1)
    model_completed: bool = Field(default=True, description="模型检查是否完成")
