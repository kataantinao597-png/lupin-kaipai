"""0006: 事实生命周期——状态机六态 + 变更审计 + 内容版本快照（S6.1）。

- fact_status_history（S6.1）：状态变更审计（操作者/时间/原因/证据）。
- fact_versions（S6.1）：事实内容变更前快照（与 work_versions 同模式）。
- 数据迁移：历史 "pending" 改写为 "pending_review"（S6.1 六态工作流）。
- facts.status server_default 改为 "draft"（新事实初始状态）。

Revision ID: 0006_fact_lifecycle
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_fact_lifecycle"
down_revision: Union[str, None] = "0005_review_delivery_closure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # ---- S6.1：fact_status_history ----
    if "fact_status_history" not in tables:
        op.create_table(
            "fact_status_history",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("fact_id", sa.String(32),
                      sa.ForeignKey("facts.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("org_id", sa.String(32), nullable=False),
            sa.Column("from_status", sa.String(16), nullable=False),
            sa.Column("to_status", sa.String(16), nullable=False),
            sa.Column("operator_id", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("evidence_ref", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False,
                      server_default=sa.func.now()),
            sa.Index("ix_facthist_fact", "fact_id"),
            sa.Index("ix_facthist_org", "org_id"),
        )

    # ---- S6.1：fact_versions ----
    if "fact_versions" not in tables:
        op.create_table(
            "fact_versions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("fact_id", sa.String(32),
                      sa.ForeignKey("facts.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("category", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("source_type", sa.String(32), nullable=False,
                      server_default=""),
            sa.Column("source_ref", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default=""),
            sa.Column("allow_use", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("fact_id", "version_no",
                                name="uq_factversion_no"),
            sa.Index("ix_factversion_fact", "fact_id"),
        )

    # ---- S6.1：历史 "pending" 改写为 "pending_review" ----
    if "facts" in tables:
        conn.execute(sa.text(
            "UPDATE facts SET status='pending_review' "
            "WHERE status='pending'"))

    # ---- S6.1：facts.status server_default 改为 draft ----
    # SQLite 需 batch 模式重建表；PostgreSQL 直接 ALTER。
    with op.batch_alter_table("facts") as batch:
        batch.alter_column("status", server_default="draft")


def downgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)
    if "fact_versions" in tables:
        op.drop_table("fact_versions")
    if "fact_status_history" in tables:
        op.drop_table("fact_status_history")
    # 数据改写不回滚（pending_review 改回 pending 会丢失语义区分）；
    # server_default 回滚：
    with op.batch_alter_table("facts") as batch:
        batch.alter_column("status", server_default="pending_review")
