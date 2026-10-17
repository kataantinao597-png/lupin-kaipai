"""SQLAlchemy ORM 模型：组织 / 成员 / 邀请 / 作品。

与认证组共用 backend/data/lupin.db，Base 取自 backend.auth.models，
保证同一 metadata 下 create_all 能建出全部表。

注意：User 表由认证组负责建模与维护；此处 owner_id / user_id
均为字符串关联（User.id 是 str），不做 DB 级外键，避免耦合。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Text, UniqueConstraint, Index

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
    created_by = Column(String(64), nullable=False)  # 创建者 user_id
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
    user_id = Column(String(64), nullable=False)
    org_id = Column(String(32), nullable=False)
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
    org_id = Column(String(32), nullable=False)
    code = Column(String(24), nullable=False, unique=True)
    # 被邀请者加入后的默认角色
    role = Column(String(16), nullable=False, default="viewer")
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_by = Column(String(64), nullable=True)


class Work(Base):
    """作品：租户隔离新体系。048

    现有 localStorage 作品暂不迁移（Phase 3 做），此表是新体系起点。
    """
    __tablename__ = "works"
    __table_args__ = (
        Index("ix_work_org", "org_id"),
        Index("ix_work_owner", "owner_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), nullable=False)  # 租户上下文，查询必须带此条件
    owner_id = Column(String(64), nullable=False)  # 创建者 user_id
    title = Column(String(256), nullable=False, default="")
    data_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)
