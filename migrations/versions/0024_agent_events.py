"""0024: Agent 过程事件独立落表（O-02 彻底方案）。

原来 _push_event 每次事件都读 task → 改 payload["events"] →
update_payload 全量写回整个 payload JSON（11 步约 30 余次/ run）。
现事件单行落 agent_events 表，一次 INSERT，不再读写 payload JSON。

- 列与 backend/services/agent/conversations.py 的 AgentEvent 保持一致；
- (task_id, seq) 唯一索引：同一 task 内 seq 单调递增，供
  get_run_state 的 ?after_seq= 增量查询走索引；
- 不做数据迁移：旧 run 的事件仍在 payload["events"] 里，
  get_run_state 在本表无行时回退读 payload。

幂等：表已存在则跳过建表（存量库重跑不炸）。

Revision ID: 0024_agent_events
Revises: 0023_idempotency_user_scope
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_agent_events"
down_revision: Union[str, None] = "0023_idempotency_user_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    if not _has_table("agent_events"):
        op.create_table(
            "agent_events",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("task_id", sa.String(32),
                      sa.ForeignKey("ai_tasks.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("seq", sa.Integer, nullable=False),
            sa.Column("type", sa.String(32), nullable=False),
            sa.Column("tool_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("note", sa.Text, nullable=True),
            sa.Column("items_json", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
        )
    if not _has_index("agent_events", "ix_agent_events_task_seq"):
        op.create_index("ix_agent_events_task_seq", "agent_events",
                        ["task_id", "seq"], unique=True)


def downgrade() -> None:
    if _has_index("agent_events", "ix_agent_events_task_seq"):
        op.drop_index("ix_agent_events_task_seq", "agent_events")
    if _has_table("agent_events"):
        op.drop_table("agent_events")
