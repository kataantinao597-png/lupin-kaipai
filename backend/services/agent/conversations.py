"""Phase 3：对话持久化（conversations / conversation_messages / agent_steps）。

表结构由迁移 0020 创建（migrations/versions/0020_agent_tables.py）；
本模块提供 ORM 映射 + 服务函数。约定：

- 全部走 ORM，不拼 SQL；
- 每次读写校验 org 归属：conversation.org_id 必须等于服务端传入的
  org_id，否则视为不存在（EntityNotFound → 路由层转 404，防枚举）；
- conversation_messages.role 限 user / assistant / tool 三种；
- content_json 只存可展示的结构化内容，不存密钥。
"""
from __future__ import annotations

# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入，先走 backend.auth.*。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Session

from ...models import Base
from ...repositories.base import EntityNotFound

__all__ = [
    "MESSAGE_ROLES",
    "Conversation",
    "ConversationMessage",
    "AgentStep",
    "create_conversation",
    "get_conversation",
    "get_conversation_or_404",
    "list_conversations",
    "set_conversation_status",
    "append_message",
    "list_messages",
    "count_messages",
]

# conversation_messages.role 允许值（与 0020 的 CHECK 约束一致）
MESSAGE_ROLES = ("user", "assistant", "tool")


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ORM 映射（列定义与迁移 0020 保持一致）
# ---------------------------------------------------------------------------

class Conversation(Base):
    """多轮对话会话：关联组织 / 用户 / 作品 + brief。"""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_org", "org_id"),
        Index("ix_conversations_user", "user_id"),
        Index("ix_conversations_work", "work_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32),
                    ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    user_id = Column(String(64), nullable=False)
    work_id = Column(String(64), nullable=True)
    brief_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now)


class ConversationMessage(Base):
    """对话消息：role ∈ user / assistant / tool。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conv", "conversation_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    conversation_id = Column(String(32),
                             ForeignKey("conversations.id",
                                        ondelete="CASCADE"),
                             nullable=False)
    role = Column(String(16), nullable=False)
    content_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now)


class AgentStep(Base):
    """工具级执行记录：挂在 ai_tasks.id 下。"""

    __tablename__ = "agent_steps"
    __table_args__ = (
        Index("ix_agent_steps_task", "task_id"),
        Index("ix_agent_steps_tool", "tool_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    task_id = Column(String(32),
                     ForeignKey("ai_tasks.id", ondelete="CASCADE"),
                     nullable=False)
    tool_id = Column(String(64), nullable=False)
    # params 的 sha256 hex（去重键的一部分）
    params_digest = Column(String(64), nullable=False)
    output_ref = Column(String(256), nullable=True)
    # pending | running | succeeded | failed | waiting_user | cancelled | skipped
    status = Column(String(16), nullable=False, default="pending")
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)


# ---------------------------------------------------------------------------
# 会话 CRUD（全部带 org 归属校验）
# ---------------------------------------------------------------------------

def create_conversation(db: Session, org_id: str, user_id: str,
                        work_id: Optional[str] = None,
                        brief: Optional[dict[str, Any]] = None,
                        status: str = "active") -> Conversation:
    """建会话。brief 只存脱敏后的结构化摘要，不存密钥。"""
    conv = Conversation(
        org_id=org_id, user_id=user_id, work_id=work_id,
        brief_json=dict(brief or {}), status=status,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def get_conversation(db: Session, conversation_id: str,
                     org_id: str) -> Optional[Conversation]:
    """取会话；org 不匹配返回 None（调用方转 404，防枚举）。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.org_id != org_id:
        return None
    return conv


def get_conversation_or_404(db: Session, conversation_id: str,
                            org_id: str) -> Conversation:
    conv = get_conversation(db, conversation_id, org_id)
    if conv is None:
        raise EntityNotFound("Conversation", conversation_id)
    return conv


def list_conversations(db: Session, org_id: str,
                       user_id: Optional[str] = None,
                       work_id: Optional[str] = None,
                       status: Optional[str] = None,
                       limit: int = 50, offset: int = 0) -> list[Conversation]:
    """按组织列会话（可选按用户/作品/状态过滤），创建时间倒序。"""
    q = db.query(Conversation).filter(Conversation.org_id == org_id)
    if user_id is not None:
        q = q.filter(Conversation.user_id == user_id)
    if work_id is not None:
        q = q.filter(Conversation.work_id == work_id)
    if status is not None:
        q = q.filter(Conversation.status == status)
    return list(q.order_by(Conversation.created_at.desc())
                 .limit(max(1, min(limit, 200)))
                 .offset(max(0, offset))
                 .all())


def set_conversation_status(db: Session, conversation_id: str, org_id: str,
                            status: str) -> Conversation:
    """改会话状态（如 active → archived）。org 不匹配抛 EntityNotFound。"""
    conv = get_conversation_or_404(db, conversation_id, org_id)
    conv.status = status
    db.commit()
    db.refresh(conv)
    return conv


# ---------------------------------------------------------------------------
# 消息：追加 / 分页读取
# ---------------------------------------------------------------------------

def append_message(db: Session, conversation_id: str, org_id: str,
                   role: str,
                   content: dict[str, Any]) -> ConversationMessage:
    """追加一条消息。

    - role 不在 (user, assistant, tool) → ValueError；
    - 会话不存在或不属于该 org → EntityNotFound（404 防枚举）。
    """
    if role not in MESSAGE_ROLES:
        raise ValueError(
            f"非法 role: {role!r}（允许 {list(MESSAGE_ROLES)}）")
    if not isinstance(content, dict):
        raise ValueError("content 必须是 JSON 对象")
    get_conversation_or_404(db, conversation_id, org_id)
    msg = ConversationMessage(
        conversation_id=conversation_id, role=role,
        content_json=dict(content),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def list_messages(db: Session, conversation_id: str, org_id: str,
                  limit: int = 50,
                  offset: int = 0) -> list[ConversationMessage]:
    """分页读消息（时间正序，offset/limit）。org 不匹配抛 EntityNotFound。"""
    get_conversation_or_404(db, conversation_id, org_id)
    q = (db.query(ConversationMessage)
           .filter(ConversationMessage.conversation_id == conversation_id)
           .order_by(ConversationMessage.created_at.asc(),
                     ConversationMessage.id.asc()))
    return list(q.limit(max(1, min(limit, 200)))
                 .offset(max(0, offset))
                 .all())


def count_messages(db: Session, conversation_id: str,
                   org_id: str) -> int:
    """消息总数（分页用）。org 不匹配抛 EntityNotFound。"""
    get_conversation_or_404(db, conversation_id, org_id)
    return (db.query(ConversationMessage)
              .filter(ConversationMessage.conversation_id == conversation_id)
              .count())
