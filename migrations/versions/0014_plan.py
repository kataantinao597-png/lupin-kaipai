"""0014: 内容策划工作台 content_tasks 表（V3.0 C）。

- 一次"营销目标→候选创意→选中→关联拍摄项目"的策划任务。
- 不碰任何现有表。

Revision ID: 0014_plan
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_plan"
down_revision: Union[str, None] = "0013_copyopt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("content_goal", sa.String(16), nullable=False,
                  server_default="brand"),
        sa.Column("product_id", sa.String(32),
                  sa.ForeignKey("products.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("audience", sa.String(256), nullable=False, server_default=""),
        sa.Column("content_purpose", sa.String(256), nullable=False,
                  server_default=""),
        sa.Column("core_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("selling_points", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("style", sa.String(128), nullable=False, server_default=""),
        sa.Column("target_duration_seconds", sa.Integer(), nullable=False,
                  server_default="30"),
        sa.Column("target_platforms", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("shoot_conditions", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("ad_restrictions", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("candidates", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("selected_index", sa.Integer(), nullable=True),
        sa.Column("selected_idea", sa.JSON(), nullable=True),
        sa.Column("work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="draft"),
        sa.Column("ai_meta", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_contenttask_org", "content_tasks", ["org_id"])
    op.create_index("ix_contenttask_goal", "content_tasks",
                    ["org_id", "content_goal"])
    op.create_index("ix_contenttask_work", "content_tasks", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_contenttask_work", table_name="content_tasks")
    op.drop_index("ix_contenttask_goal", table_name="content_tasks")
    op.drop_index("ix_contenttask_org", table_name="content_tasks")
    op.drop_table("content_tasks")
