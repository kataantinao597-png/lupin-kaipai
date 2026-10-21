"""0020: Agent 对话持久化最小表（Phase 2）。

新增三张表（PHASE1_DESIGN.md §2 最小化结论）：
- conversations：多轮对话会话（id/org_id/user_id/work_id/brief_json/status/created_at）
- conversation_messages：消息历史（id/conversation_id/role/content_json/created_at，
  role 限 user/assistant/tool）
- agent_steps：工具级执行记录（id/task_id→ai_tasks.id/tool_id/params_digest/
  output_ref/status/error/started_at/finished_at/duration_ms）

不新增 agent_runs 表：run 直接建模为 AITask(task_type="agent_run")（§2 结论）。
SQLite/Postgres 兼容：sa.JSON、server_default=func.now()、显式 FK ondelete。

幂等：已存在表则跳过建表（存量库重跑不炸）。

Revision ID: 0020_agent_tables
Revises: 0019_ai_call_org
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_agent_tables"
down_revision: Union[str, None] = "0019_ai_call_org"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    if not _has_table("conversations"):
        op.create_table(
            "conversations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("work_id", sa.String(64), nullable=True),
            sa.Column("brief_json", sa.JSON, nullable=False,
                      server_default="{}"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_conversations_org", "org_id"),
            sa.Index("ix_conversations_user", "user_id"),
            sa.Index("ix_conversations_work", "work_id"),
        )
    if not _has_table("conversation_messages"):
        op.create_table(
            "conversation_messages",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("conversation_id", sa.String(32),
                      sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content_json", sa.JSON, nullable=False,
                      server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "role IN ('user', 'assistant', 'tool')",
                name="ck_conversation_messages_role"),
            sa.Index("ix_conversation_messages_conv", "conversation_id"),
        )
    if not _has_table("agent_steps"):
        op.create_table(
            "agent_steps",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("task_id", sa.String(32),
                      sa.ForeignKey("ai_tasks.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("tool_id", sa.String(64), nullable=False),
            sa.Column("params_digest", sa.String(64), nullable=False),
            sa.Column("output_ref", sa.String(256), nullable=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="pending"),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Index("ix_agent_steps_task", "task_id"),
            sa.Index("ix_agent_steps_tool", "tool_id"),
        )
    # 存量库「表已存在但索引缺失」的补齐（幂等）
    if not _has_index("conversations", "ix_conversations_org"):
        op.create_index("ix_conversations_org", "conversations", ["org_id"])
    if not _has_index("conversation_messages", "ix_conversation_messages_conv"):
        op.create_index("ix_conversation_messages_conv",
                        "conversation_messages", ["conversation_id"])
    if not _has_index("agent_steps", "ix_agent_steps_task"):
        op.create_index("ix_agent_steps_task", "agent_steps", ["task_id"])


def downgrade() -> None:
    for table in ("agent_steps", "conversation_messages", "conversations"):
        if _has_table(table):
            op.drop_table(table)
