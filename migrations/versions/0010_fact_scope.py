"""0010: 事实表加适用范围字段 scope。

- facts.scope：VARCHAR(128)，NOT NULL，默认 ""（老数据自动填充空串）。
- 仅展示用，不参与业务逻辑。

Revision ID: 0010_fact_scope
Replaces: None
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_fact_scope"
down_revision: Union[str, None] = "0007_security_phase9"  # 当前链头（0007_security_phase9 挂在 0009 之后）
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts",
        sa.Column("scope", sa.String(128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("facts", "scope")
