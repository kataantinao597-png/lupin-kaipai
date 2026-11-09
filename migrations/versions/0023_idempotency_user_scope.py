"""0023: 幂等键按用户隔离（S-2 彻底隔离）。

终审 S-2：幂等行查找原先只按 (route, key)，scope 哈希虽已堵住跨用户
回放（不同用户同 key → body_hash 不一致 → 400 冲突），但 key 占用面
仍在，且旧唯一约束 (route, key) 从库层面禁止同一 key 被两个用户各建
一行。本迁移把隔离收进库约束：

- 加 user_id 列（可空；旧行保持 NULL，靠 body_hash 过渡认领，见
  backend/core/idempotency.py 的 _try_adopt_legacy_row，不回填——旧行
  的 body_hash 含用户 scope，认领时比对哈希即可确认归属）；
- 删旧唯一约束 uq_idempotency_keys_route_key（0021 建表时建）；
- 建两条部分唯一索引（SQLite/Postgres 均把唯一索引中的 NULL 视为互不
  相等，故必须分两条，否则匿名 NULL 行会失去唯一性）：
  - ix_idem_route_key_user：(route, key, user_id) WHERE user_id IS NOT NULL
  - ix_idem_route_key_anon：(route, key) WHERE user_id IS NULL
- 另建 ix_idem_user 供按用户清理/审计。

幂等：列/索引已存在则跳过（存量库重跑不炸）；SQLite 下删唯一约束
走 batch 模式（表重建，数据保留），Postgres 直接删约束。
旧约束名若已被 create_all 式建库替换为 ix_idem_route_key 唯一索引，
一并处理（删索引）。

Revision ID: 0023_idempotency_user_scope
Revises: 0022_poster_image_usage
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_idempotency_user_scope"
down_revision: Union[str, None] = "0022_poster_image_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {i["name"] for i in insp.get_indexes(table)}


def _has_unique(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {c["name"]
                    for c in insp.get_unique_constraints(table)}


def upgrade() -> None:
    if not _has_table("idempotency_keys"):
        return
    if not _has_column("idempotency_keys", "user_id"):
        op.add_column("idempotency_keys",
                      sa.Column("user_id", sa.String(64), nullable=True))
    # 删旧 (route, key) 唯一约束/索引（两者只会存在其一）
    if _has_unique("idempotency_keys", "uq_idempotency_keys_route_key"):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("idempotency_keys") as batch:
                batch.drop_constraint("uq_idempotency_keys_route_key",
                                      type_="unique")
        else:
            op.drop_constraint("uq_idempotency_keys_route_key",
                               "idempotency_keys", type_="unique")
    if _has_index("idempotency_keys", "ix_idem_route_key"):
        op.drop_index("ix_idem_route_key", "idempotency_keys")
    # 两条部分唯一索引（旧行 user_id 全为 NULL，不会与新索引冲突；
    # 旧约束已保证 (route, key) 无重复，故 anon 索引建表必成功）
    if not _has_index("idempotency_keys", "ix_idem_route_key_user"):
        op.create_index(
            "ix_idem_route_key_user", "idempotency_keys",
            ["route", "key", "user_id"], unique=True,
            postgresql_where=sa.text("user_id IS NOT NULL"),
            sqlite_where=sa.text("user_id IS NOT NULL"))
    if not _has_index("idempotency_keys", "ix_idem_route_key_anon"):
        op.create_index(
            "ix_idem_route_key_anon", "idempotency_keys",
            ["route", "key"], unique=True,
            postgresql_where=sa.text("user_id IS NULL"),
            sqlite_where=sa.text("user_id IS NULL"))
    if not _has_index("idempotency_keys", "ix_idem_user"):
        op.create_index("ix_idem_user", "idempotency_keys", ["user_id"])


def downgrade() -> None:
    if not _has_table("idempotency_keys"):
        return
    for name in ("ix_idem_route_key_user", "ix_idem_route_key_anon",
                 "ix_idem_user"):
        if _has_index("idempotency_keys", name):
            op.drop_index(name, "idempotency_keys")
    if not _has_unique("idempotency_keys", "uq_idempotency_keys_route_key"):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("idempotency_keys") as batch:
                batch.create_unique_constraint(
                    "uq_idempotency_keys_route_key", ["route", "key"])
        else:
            op.create_unique_constraint("uq_idempotency_keys_route_key",
                                        "idempotency_keys", ["route", "key"])
    if _has_column("idempotency_keys", "user_id"):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("idempotency_keys") as batch:
                batch.drop_column("user_id")
        else:
            op.drop_column("idempotency_keys", "user_id")
