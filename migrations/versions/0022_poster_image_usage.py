"""0022: 海报 AI 生图用量台账（普通用户每天 5 次限额）。

`poster_image_usage` 表（user_id/org_id/created_at）：每次成功调用
POST /api/v1/poster/image 记一条；限额检查按 user_id + 当天计数。

幂等：表已存在则跳过建表（存量库重跑不炸）。

Revision ID: 0022_poster_image_usage
Revises: 0021_idempotency_keys
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_poster_image_usage"
down_revision: Union[str, None] = "0021_idempotency_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def upgrade() -> None:
    if not _has_table("poster_image_usage"):
        op.create_table(
            "poster_image_usage",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("org_id", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_poster_usage_user_created", "user_id", "created_at"),
            sa.Index("ix_poster_usage_org", "org_id"),
        )


def downgrade() -> None:
    if _has_table("poster_image_usage"):
        op.drop_table("poster_image_usage")
