"""SQLAlchemy ORM 模型：组织 / 成员 / 邀请 / 作品 / 分镜 / 镜头 / 事实库。

与认证组共用 Base（backend.auth.models），同一 metadata 下
`alembic upgrade head` 建出全部表（本地开发也可用 create_all 兜底）。

Phase 3（081-088）变更：
- 083：works 补 status（draft/active/archived）与 deleted_at（软删除预留）
- 084：storyboards / shots 表；shots 以 UUID 为 PK（稳定 ID），shot_no 仅展示顺序
- 085：facts 事实库表（pending/confirmed/rejected/expired + allow_use + 有效期）
- 086：全部 DB 级外键；子表 ondelete=CASCADE；works.org_id 用 RESTRICT
      防止误删组织时连带清空租户资产（需先显式删除作品）
- 088：works / storyboards / facts 加 version 乐观锁字段

注意：User 表由认证组负责；user_id / owner_id / created_by / verified_by
均为字符串逻辑关联（User.id 是 str），不做 DB 级外键，避免模块耦合。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from .auth.models import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    """企业组织。045"""
    __tablename__ = "organizations"

    id = Column(String(32), primary_key=True, default=_uuid)
    name = Column(String(128), nullable=False)
    created_by = Column(String(64), nullable=False)  # 创建者 user_id（逻辑关联）
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Membership(Base):
    """组织成员关系：一个用户可加入多个组织。045"""
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
        Index("ix_membership_org", "org_id"),
        Index("ix_membership_user", "user_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    user_id = Column(String(64), nullable=False)  # 逻辑关联 users.id
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    # owner | admin | editor | reviewer | viewer
    role = Column(String(16), nullable=False, default="viewer")
    joined_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Invitation(Base):
    """组织邀请码：7 天有效。045"""
    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitation_code", "code", unique=True),
        Index("ix_invitation_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    code = Column(String(24), nullable=False, unique=True)
    # 被邀请者加入后的默认角色
    role = Column(String(16), nullable=False, default="viewer")
    created_by = Column(String(64), nullable=False)  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_by = Column(String(64), nullable=True)


class Work(Base):
    """作品：租户隔离。083/088：状态、软删除、乐观锁版本。"""
    __tablename__ = "works"
    __table_args__ = (
        Index("ix_work_org", "org_id"),
        Index("ix_work_owner", "owner_id"),
        Index("ix_work_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    # 086：组织删除时先显式处理作品，RESTRICT 防止误删连带清空
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    owner_id = Column(String(64), nullable=False)  # 逻辑关联 users.id
    title = Column(String(256), nullable=False, default="")
    data_json = Column(Text, nullable=False, default="{}")
    # 083：draft | active | archived
    status = Column(String(16), nullable=False, default="draft", server_default="draft")
    # 083：软删除时间戳（NULL=未删除）；删除 API 后续改为写此字段
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # 088：乐观锁版本，每次更新 +1
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Storyboard(Base):
    """084：分镜版本。一个作品可有多个分镜版本，version 做乐观锁。"""
    __tablename__ = "storyboards"
    __table_args__ = (
        Index("ix_storyboard_work", "work_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    title = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Shot(Base):
    """084：镜头。id 为稳定 UUID（跨排序/重生成保持关联）；
    shot_no 只是展示顺序。shot_id_stable 承接前端已有稳定 ID。"""
    __tablename__ = "shots"
    __table_args__ = (
        Index("ix_shot_storyboard", "storyboard_id"),
        Index("ix_shot_stable", "shot_id_stable"),
        UniqueConstraint("storyboard_id", "shot_id_stable",
                         name="uq_shot_storyboard_stable"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    storyboard_id = Column(String(32), ForeignKey("storyboards.id", ondelete="CASCADE"),
                           nullable=False)
    shot_no = Column(Integer, nullable=False, default=1)
    shot_id_stable = Column(String(64), nullable=True)
    content_json = Column(JSON, nullable=False, default=dict)
    duration_seconds = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# 085：事实状态
FACT_STATUS_PENDING = "pending"
FACT_STATUS_CONFIRMED = "confirmed"
FACT_STATUS_REJECTED = "rejected"
FACT_STATUS_EXPIRED = "expired"
FACT_STATUSES = (
    FACT_STATUS_PENDING, FACT_STATUS_CONFIRMED,
    FACT_STATUS_REJECTED, FACT_STATUS_EXPIRED,
)


class Fact(Base):
    """085：可信事实库。AI 只引用 status=confirmed 且 allow_use 的事实（010）；
    事实本身的状态流转只能由人工/确定性规则推进，模型无权自升（143）。"""
    __tablename__ = "facts"
    __table_args__ = (
        Index("ix_fact_org", "org_id"),
        Index("ix_fact_product", "product_id"),
        Index("ix_fact_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    product_id = Column(String(64), nullable=False, default="")
    content = Column(Text, nullable=False)
    category = Column(String(64), nullable=False, default="")
    # official_document | enterprise_material | human_statement | ai_inference
    source_type = Column(String(32), nullable=False, default="human_statement")
    source_ref = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default=FACT_STATUS_PENDING,
                    server_default=FACT_STATUS_PENDING)
    allow_use = Column(Boolean, nullable=False, default=False, server_default="0")
    verified_by = Column(String(64), nullable=True)  # 逻辑关联 users.id
    verified_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # 088：乐观锁版本
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class WorkVersion(Base):
    """091：作品历史快照（兄弟组 API 已引用，此处补模型定义）。

    每次 data 实质变更前存一条，version_no 单调递增；超限删最旧。
    """
    __tablename__ = "work_versions"
    __table_args__ = (
        Index("ix_workversion_work", "work_id"),
        UniqueConstraint("work_id", "version_no", name="uq_workversion_no"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    version_no = Column(Integer, nullable=False)
    snapshot_json = Column(Text, nullable=False, default="{}")
    title = Column(String(256), nullable=False, default="")
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
