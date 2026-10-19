"""0013: 文案优化器 copy_docs / copy_versions 表（V3.0 D）。

- 独立文案优化器的文档 + 轻量版本表（只读追加，用户未确认不覆盖）。
- 不碰任何现有表。

Revision ID: 0013_copyopt
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_copyopt"
down_revision: Union[str, None] = "0012_product_refs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "copy_docs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False,
                  server_default="optimize"),
        sa.Column("product_id", sa.String(32),
                  sa.ForeignKey("products.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_copydoc_org", "copy_docs", ["org_id"])
    op.create_index("ix_copydoc_kind", "copy_docs", ["org_id", "kind"])

    op.create_table(
        "copy_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("doc_id", sa.String(32),
                  sa.ForeignKey("copy_docs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False,
                  server_default="natural"),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="draft"),
        sa.Column("source_text", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("structured", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("goal", sa.String(256), nullable=False, server_default=""),
        sa.Column("style", sa.String(128), nullable=False, server_default=""),
        sa.Column("audience", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("length_limit", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("extra_requirements", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("custom_instruction", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("changes_summary", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("fact_ids", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("risk_status", sa.String(16), nullable=False,
                  server_default="unchecked"),
        sa.Column("risk_flags", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("risk_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_meta", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("restored_from", sa.String(32), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # 唯一约束内联建表（SQLite 不支持 ALTER 加约束）
        sa.UniqueConstraint("doc_id", "version_no", name="uq_copyver_no"),
    )
    op.create_index("ix_copyver_org", "copy_versions", ["org_id"])
    op.create_index("ix_copyver_doc", "copy_versions", ["doc_id"])


def downgrade() -> None:
    op.drop_table("copy_versions")
    op.drop_table("copy_docs")
