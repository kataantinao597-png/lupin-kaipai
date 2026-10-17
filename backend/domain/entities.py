"""025：独立领域模型。

领域实体（Entity）与 SQLAlchemy ORM 模型分离：

- ORM 模型（backend/models.py）：数据库表映射，含 Column/Index/FK 等持久化细节
- 领域实体（本文件）：纯业务对象，用 Pydantic 定义，含业务规则方法

依赖方向：repositories → domain → (无)。ORM → domain 的转换在 Repository 内部完成，
services/api 只接触领域实体，不直接碰 ORM 对象。

当前先做 Work / Fact / Shot / Storyboard 四个核心实体；
Organization / Review / AITask 等留 TODO（039 接入时补）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Work(BaseModel):
    """作品领域实体。"""

    id: str
    org_id: str
    owner_id: str
    title: str = ""
    status: str = "draft"  # draft | active | archived
    deleted_at: Optional[datetime] = None
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_active(self) -> bool:
        return self.status == "active" and not self.is_deleted


class Fact(BaseModel):
    """可信事实领域实体。

    业务规则（143）：AI 只引用 status=confirmed 且 allow_use 的事实；
    状态流转只能由人工/确定性规则推进。
    """

    id: str
    org_id: str
    product_id: str = ""
    content: str
    category: str = ""
    source_type: str = "human_statement"
    source_ref: str = ""
    status: str = "pending"  # pending | confirmed | rejected | expired
    allow_use: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    version: int = 1

    model_config = {"from_attributes": True}

    @property
    def is_usable(self) -> bool:
        """010：可被 AI 引用的事实 = 已确认 + 允许使用 + 未过期。"""
        if self.status != "confirmed" or not self.allow_use:
            return False
        if self.expires_at is not None:
            now = datetime.now(self.expires_at.tzinfo) if self.expires_at.tzinfo else datetime.now()
            if self.expires_at < now:
                return False
        return True


class Shot(BaseModel):
    """镜头领域实体。id 为稳定 UUID（跨排序/重生成保持关联）。"""

    id: str
    storyboard_id: str
    shot_no: int = 1
    shot_id_stable: Optional[str] = None
    content: dict = Field(default_factory=dict)
    duration_seconds: int = 0
    locked: bool = False

    model_config = {"from_attributes": True}

    @property
    def stable_id(self) -> str:
        return self.shot_id_stable or self.id


class Storyboard(BaseModel):
    """分镜领域实体（含镜头列表的聚合根）。"""

    id: str
    work_id: str
    version: int = 1
    title: str = ""
    shots: list[Shot] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    @property
    def total_seconds(self) -> int:
        return sum(s.duration_seconds for s in self.shots)

    def get_shot(self, shot_no: int) -> Optional[Shot]:
        for s in self.shots:
            if s.shot_no == shot_no:
                return s
        return None


# TODO(039)：Organization / Membership / Review / AITask 领域实体待补。
# 届时与 repositories/review_repository.py、ai_task 部分一起落地。
