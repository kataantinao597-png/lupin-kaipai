"""0012: 产品档案引用标记表 product_refs（V3.0 B-5）。

- 记录作品内容引用产品档案时的产品版本号。
- 产品资料变更后只标记 stale，不改写历史内容。
- 不碰任何现有表。

Revision ID: 0012_product_refs
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_product_refs"
down_revision: Union[str, None] = "0011_product"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_refs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("product_id", sa.String(32),
                  sa.ForeignKey("products.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ref_type", sa.String(16), nullable=False,
                  server_default="work"),
        sa.Column("ref_id", sa.String(32), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("product_name", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("stale", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Index("ix_productref_org", "org_id"),
        sa.Index("ix_productref_product", "product_id"),
        sa.Index("ix_productref_ref", "ref_type", "ref_id"),
    )


def downgrade() -> None:
    op.drop_table("product_refs")
