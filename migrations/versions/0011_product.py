"""0011: 产品档案表 products（V3.0 B-1）。

- products：跨项目复用的产品资料中心。组织内 code 唯一。
- 不碰任何现有表；facts.product_id 自由字符串保持不变（B-2 兼容）。
- 删除语义由 API 层保证（有 facts 引用时禁止删除），DB 不建外键。

Revision ID: 0011_product
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_product"
down_revision: Union[str, None] = "0010_fact_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False, server_default=""),
        sa.Column("origin", sa.String(128), nullable=False, server_default=""),
        sa.Column("brand_profile", sa.Text(), nullable=False, server_default=""),
        sa.Column("brand_tone", sa.String(128), nullable=False, server_default=""),
        sa.Column("selling_points", sa.JSON(), nullable=False,
                  server_default="[]"),
        sa.Column("ad_restrictions", sa.Text(), nullable=False, server_default=""),
        sa.Column("no_expand_claims", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "code", name="uq_product_org_code"),
    )
    op.create_index("ix_product_org", "products", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_product_org", table_name="products")
    op.drop_table("products")
