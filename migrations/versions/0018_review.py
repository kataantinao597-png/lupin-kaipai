"""0018: 效果复盘（V3.0 H）。

- 新建 effect_metrics：效果指标录入批次（一次录入一条，支持多次追加；
  每条必带来源 manual/screenshot + 数据起止日期；metrics 灵活 JSON）。
- 新建 review_suggestions：AI 改进建议（只存文本+状态，绝不自动覆盖
  已确认内容/已发布版本）。
- publish_records 加 review_note（复盘结论，人工撰写）与 reviewed_at。

Revision ID: 0018_review
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_review"
down_revision: Union[str, None] = "0017_asset_refs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "effect_metrics",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("publish_record_id", sa.String(32),
                  sa.ForeignKey("publish_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(32), nullable=False,
                  server_default="manual"),
        sa.Column("date_start", sa.Date(), nullable=True),
        sa.Column("date_end", sa.Date(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False,
                  server_default="{}"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_em_org", "effect_metrics", ["org_id"])
    op.create_index("ix_em_rec", "effect_metrics", ["publish_record_id"])
    op.create_index("ix_em_org_rec", "effect_metrics",
                    ["org_id", "publish_record_id"])

    op.create_table(
        "review_suggestions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("publish_record_id", sa.String(32),
                  sa.ForeignKey("publish_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("suggestion_text", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("ai_meta", sa.JSON(), nullable=False,
                  server_default="{}"),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_rs_org", "review_suggestions", ["org_id"])
    op.create_index("ix_rs_rec", "review_suggestions", ["publish_record_id"])
    op.create_index("ix_rs_org_rec", "review_suggestions",
                    ["org_id", "publish_record_id"])

    op.add_column("publish_records",
                  sa.Column("review_note", sa.Text(), nullable=False,
                            server_default=""))
    op.add_column("publish_records",
                  sa.Column("reviewed_at", sa.DateTime(timezone=True),
                            nullable=True))


def downgrade() -> None:
    op.drop_column("publish_records", "reviewed_at")
    op.drop_column("publish_records", "review_note")
    op.drop_index("ix_rs_org_rec", table_name="review_suggestions")
    op.drop_index("ix_rs_rec", table_name="review_suggestions")
    op.drop_index("ix_rs_org", table_name="review_suggestions")
    op.drop_table("review_suggestions")
    op.drop_index("ix_em_org_rec", table_name="effect_metrics")
    op.drop_index("ix_em_rec", table_name="effect_metrics")
    op.drop_index("ix_em_org", table_name="effect_metrics")
    op.drop_table("effect_metrics")
