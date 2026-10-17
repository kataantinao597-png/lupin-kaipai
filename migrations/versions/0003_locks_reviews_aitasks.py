"""0003: 镜头锁定列 + reviews 审核表 + ai_tasks 异步任务表（032/035）。

- shots.locked（032）：镜头锁定状态持久化，默认 False。
- reviews（032/201）：审核状态机，结论绑定 content_hash。
- ai_tasks（035/036）：AI 异步任务，状态机 pending→running→succeeded/failed/cancelled。

Revision ID: 0003_locks_reviews_aitasks
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_locks_reviews_aitasks"
down_revision: Union[str, None] = "0002_work_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _indexes(conn, table: str) -> set:
    return {i["name"] for i in sa.inspect(conn).get_indexes(table)}


def _create_index_if_missing(conn, name: str, table: str, cols, unique=False):
    if table in _tables(conn) and name not in _indexes(conn, table):
        op.create_index(name, table, cols, unique=unique)


def upgrade() -> None:
    conn = op.get_bind()

    # ---- 032：shots.locked ----
    if "shots" in _tables(conn) and "locked" not in _columns(conn, "shots"):
        op.add_column(
            "shots",
            sa.Column("locked", sa.Boolean(), nullable=False,
                      server_default="0"),
        )

    # ---- 032/201：reviews 表 ----
    if "reviews" not in _tables(conn):
        op.create_table(
            "reviews",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("work_id", sa.String(32),
                      sa.ForeignKey("works.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("storyboard_id", sa.String(32),
                      sa.ForeignKey("storyboards.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("content_hash", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="pending"),
            sa.Column("decisions_json", sa.JSON(), nullable=False,
                      server_default="{}"),
            sa.Column("reviewer_id", sa.String(64), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("comment", sa.Text(), nullable=False, server_default=""),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing(conn, "ix_review_work", "reviews", ["work_id"])
    _create_index_if_missing(conn, "ix_review_status", "reviews", ["status"])

    # ---- 035/036：ai_tasks 表 ----
    if "ai_tasks" not in _tables(conn):
        op.create_table(
            "ai_tasks",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("task_type", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="pending"),
            sa.Column("payload_json", sa.JSON(), nullable=False,
                      server_default="{}"),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("progress", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("model_used", sa.String(128), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing(conn, "ix_aitask_org", "ai_tasks", ["org_id"])
    _create_index_if_missing(conn, "ix_aitask_status", "ai_tasks", ["status"])
    _create_index_if_missing(conn, "ix_aitask_created", "ai_tasks",
                             ["created_at"])


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    for ix_name, table in (("ix_aitask_created", "ai_tasks"),
                           ("ix_aitask_status", "ai_tasks"),
                           ("ix_aitask_org", "ai_tasks"),
                           ("ix_review_status", "reviews"),
                           ("ix_review_work", "reviews")):
        if table in existing and ix_name in _indexes(conn, table):
            op.drop_index(ix_name, table_name=table)
    if "ai_tasks" in existing:
        op.drop_table("ai_tasks")
    if "reviews" in existing:
        op.drop_table("reviews")
    if "shots" in existing and "locked" in _columns(conn, "shots"):
        op.drop_column("shots", "locked")
