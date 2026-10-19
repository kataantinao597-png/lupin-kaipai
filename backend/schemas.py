"""鲁品开拍 - Pydantic 数据模型（对应实验 schema.json 的字段与枚举）。"""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

SHOT_TYPES = ["特写", "近景", "中景", "中远景", "全景", "远景"]


class ShootInfo(BaseModel):
    device: str = Field(..., min_length=1)
    people: int = Field(..., ge=1)
    lighting: Optional[str] = ""
    location: Optional[str] = ""


class FailureMode(BaseModel):
    """单个镜头的常见失败情况及解决步骤（v2 新增）。"""
    symptom: str = Field(default="", description="失败现象（必须是可观察的，如'画面抖动''主体虚焦'）")
    fix: str = Field(default="", description="解决步骤或替代拍法（具体可操作）")


class Shot(BaseModel):
    shot_number: int = Field(..., ge=1)
    duration_seconds: float = Field(..., gt=0,
        description="兼容字段：v1 含义=镜头时长；v2 中等同于 final_duration_seconds（成片使用时长）")
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

    # ---------- v2 新增：时长拆分 ----------
    record_duration_seconds: Optional[float] = Field(default=None, gt=0,
        description="建议现场录制时长（秒）：单段原始视频要录多久，应≥成片使用时长")
    usable_duration_seconds: Optional[float] = Field(default=None, gt=0,
        description="预计可用素材时长（秒）：录制后能剪辑使用的部分")
    final_duration_seconds: Optional[float] = Field(default=None, gt=0,
        description="成片使用时长（秒）：该镜头在最终视频中的时长；缺省时回退 duration_seconds")

    # ---------- v2 新增：可执行指导 ----------
    shot_purpose: str = Field(default="", description="镜头目的/叙事作用（这镜要完成什么表达任务）")
    prerequisites: str = Field(default="", description="前置条件及所需素材（拍这镜之前必须备好的）")
    shoot_steps: List[str] = Field(default_factory=list,
        description="按顺序的具体拍摄步骤（每一步都是可执行的动作）")
    phone_setup: str = Field(default="",
        description="手机摆放方式、主体在画面中的位置、建议拍摄距离、光线方向")
    acceptance_criteria: List[str] = Field(default_factory=list,
        description="画面验收标准（必须是可观察的具体标准，如'樱桃占画面1/3以上'）")
    failure_modes: List[FailureMode] = Field(default_factory=list,
        description="常见失败情况+解决步骤（至少1个）")
    reshoot_guide: str = Field(default="", description="补拍指引（验收不通过时怎么补拍）")
    alt_approach: str = Field(default="", description="替代拍法（原方案不可行时的备选）")
    transition: str = Field(default="", description="转场说明（与上一个/下一个镜头的衔接方式）")
    edit_notes: str = Field(default="", description="剪辑说明（素材怎么用、节奏、音效建议）")

    @property
    def effective_final_seconds(self) -> float:
        """成片使用时长：优先 final_duration_seconds，缺省回退 duration_seconds（v1 兼容）。"""
        return self.final_duration_seconds if self.final_duration_seconds is not None \
            else self.duration_seconds


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
