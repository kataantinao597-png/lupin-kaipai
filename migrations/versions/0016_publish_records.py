"""0016: 发布中心 publish_records 表（V3.0 F）。

- 为每次发布建立独立记录；关联源拍摄项目/文案版本/平台版本/视频与封面文件。
- 不碰任何现有表。

Revision ID: 0016_publish_records
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_publish_records"
down_revision: Union[str, None] = "0015_platform_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publish_records",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("copy_version_id", sa.String(32),
                  sa.ForeignKey("copy_versions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("platform_version_id", sa.String(32),
                  sa.ForeignKey("platform_versions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("video_asset_id", sa.String(32),
                  sa.ForeignKey("assets.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("cover_asset_id", sa.String(32),
                  sa.ForeignKey("assets.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("platform", sa.String(32), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("check_result", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorization_confirmed", sa.Boolean(), nullable=False,
                  server_default="0"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_url", sa.String(1024), nullable=False,
                  server_default=""),
        sa.Column("post_metrics", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_pubrec_org", "publish_records", ["org_id"])
    op.create_index("ix_pubrec_org_status", "publish_records",
                    ["org_id", "status"])
    op.create_index("ix_pubrec_work", "publish_records", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_pubrec_work", table_name="publish_records")
    op.drop_index("ix_pubrec_org_status", table_name="publish_records")
    op.drop_index("ix_pubrec_org", table_name="publish_records")
    op.drop_table("publish_records")
