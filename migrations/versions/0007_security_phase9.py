"""0007: 安全纵深——集成 API Key（服务账号）+ 审计日志（Phase 9-4/9-5）。

- api_keys：只存 key_hash（SHA256），scopes 为 JSON 列表。
- audit_events：关键操作审计（org_id, created_at）复合索引供管理员查询。

Revision ID: 0007_security_phase9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_security_phase9"
# Phase 9 与并行组（媒体 0007_media / 网关扩展 0008 / 企业 0009）同分支开发，
# 为保持单 head，挂到当前最新 head 之后；表独立无冲突。
down_revision: Union[str, None] = "0009_enterprise"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # ---- 9-4：api_keys ----
    if "api_keys" not in tables:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("key_prefix", sa.String(16), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_apikey_org", "org_id"),
            sa.Index("ix_apikey_hash", "key_hash", unique=True),
        )

    # ---- 9-5：audit_events ----
    if "audit_events" not in tables:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32), nullable=True),
            sa.Column("user_id", sa.String(64), nullable=True),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("object_type", sa.String(32), nullable=True),
            sa.Column("object_id", sa.String(64), nullable=True),
            sa.Column("detail_json", sa.JSON(), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_audit_org_created", "org_id", "created_at"),
            sa.Index("ix_audit_action", "action"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)
    if "audit_events" in tables:
        op.drop_table("audit_events")
    if "api_keys" in tables:
        op.drop_table("api_keys")
