"""0017: 跨项目素材库 asset_refs 引用关系表（V3.0 G）。

- 引用只记关系，不复制文件：项目 B 引用项目 A 的素材，asset_refs 记一条。
- 删引用 ≠ 删实体：资产实体 DELETE 前由 API 层检查引用计数拦截，
  本迁移只建表，不碰 assets 现有删除链路。
- asset_id FK ondelete=CASCADE：实体被硬删除时引用自动清；
  dest_work_id CASCADE：目标项目被删时引用自动清；
  source_work_id SET NULL：来源项目被删时引用保留，来源记为未知。

Revision ID: 0017_asset_refs
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_asset_refs"
down_revision: Union[str, None] = "0016_publish_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "asset_refs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("asset_id", sa.String(32),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("dest_work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "asset_id", "dest_work_id",
                            name="uq_ref_org_asset_dest"),
    )
    op.create_index("ix_ref_org", "asset_refs", ["org_id"])
    op.create_index("ix_ref_asset", "asset_refs", ["asset_id"])
    op.create_index("ix_ref_dest", "asset_refs", ["dest_work_id"])
    op.create_index("ix_ref_org_asset", "asset_refs", ["org_id", "asset_id"])


def downgrade() -> None:
    op.drop_index("ix_ref_org_asset", table_name="asset_refs")
    op.drop_index("ix_ref_dest", table_name="asset_refs")
    op.drop_index("ix_ref_asset", table_name="asset_refs")
    op.drop_index("ix_ref_org", table_name="asset_refs")
    op.drop_table("asset_refs")
