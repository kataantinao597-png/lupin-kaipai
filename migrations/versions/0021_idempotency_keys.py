"""0021: DB 级幂等键表（Phase 7 必要项）。

PHASE1_DESIGN.md §1.3(5)：core/idempotency.py 是进程内存实现，多 worker/
重启失效。Agent"重复提交不重复建作品/不重复扣费"是发布硬指标，本迁移补
`idempotency_keys` 表（key/org_id/route/body_hash/status/response_json/
created_at），idempotency.py 改为 DB 实现（保持接口兼容）。

幂等：表已存在则跳过建表（存量库重跑不炸）；表预先存在但缺索引/约束时
走 batch 模式补齐（SQLite 兼容）。

Revision ID: 0021_idempotency_keys
Revises: 0020_agent_tables
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_idempotency_keys"
down_revision: Union[str, None] = "0020_agent_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {i["name"] for i in insp.get_indexes(table)}


def _has_unique(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {c["name"]
                    for c in insp.get_unique_constraints(table)}


def upgrade() -> None:
    created_now = False
    if not _has_table("idempotency_keys"):
        op.create_table(
            "idempotency_keys",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column("org_id", sa.String(32), nullable=True),
            sa.Column("route", sa.String(128), nullable=False),
            sa.Column("body_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="running"),
            sa.Column("response_json", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("route", "key",
                                name="uq_idempotency_keys_route_key"),
            sa.Index("ix_idem_org", "org_id"),
            sa.Index("ix_idem_created", "created_at"),
        )
        created_now = True
    if created_now:
        return
    # 存量库「表已存在但索引/约束缺失」的补齐（batch 模式，SQLite 兼容）
    with op.batch_alter_table("idempotency_keys") as batch:
        if not _has_unique("idempotency_keys",
                           "uq_idempotency_keys_route_key"):
            batch.create_unique_constraint(
                "uq_idempotency_keys_route_key", ["route", "key"])
        if not _has_index("idempotency_keys", "ix_idem_org"):
            batch.create_index("ix_idem_org", ["org_id"])
        if not _has_index("idempotency_keys", "ix_idem_created"):
            batch.create_index("ix_idem_created", ["created_at"])


def downgrade() -> None:
    if _has_table("idempotency_keys"):
        op.drop_table("idempotency_keys")
