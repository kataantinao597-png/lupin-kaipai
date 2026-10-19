"""0015: 多平台内容适配 platform_versions 表（V3.0 E）。

- 从已确认文案版本派生的独立平台版本；各平台版本独立保存人工调整。
- 不碰任何现有表。

Revision ID: 0015_platform_versions
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_platform_versions"
down_revision: Union[str, None] = "0014_plan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("doc_id", sa.String(32),
                  sa.ForeignKey("copy_docs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("copy_version_id", sa.String(32),
                  sa.ForeignKey("copy_versions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("source_version_no", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("hashtags", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("cover_text", sa.String(256), nullable=False,
                  server_default=""),
        sa.Column("pinned_comment", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("cta", sa.String(256), nullable=False, server_default=""),
        sa.Column("review_status", sa.String(16), nullable=False,
                  server_default="unreviewed"),
        sa.Column("changes_summary", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("fact_ids", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("risk_status", sa.String(16), nullable=False,
                  server_default="unchecked"),
        sa.Column("risk_flags", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("ai_meta", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_platver_org", "platform_versions", ["org_id"])
    op.create_index("ix_platver_doc", "platform_versions", ["doc_id"])
    op.create_index("ix_platver_doc_platform", "platform_versions",
                    ["doc_id", "platform"])


def downgrade() -> None:
    op.drop_index("ix_platver_doc_platform", table_name="platform_versions")
    op.drop_index("ix_platver_doc", table_name="platform_versions")
    op.drop_index("ix_platver_org", table_name="platform_versions")
    op.drop_table("platform_versions")
