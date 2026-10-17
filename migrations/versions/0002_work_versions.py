"""0002: work_versions 历史快照表 + 094 高频查询复合索引。

- work_versions（091）：作品修改前快照，work_id FK → works.id ON DELETE CASCADE，
  (work_id, version_no) 唯一。
- 094 复合索引：
  - ix_work_org_status ON works (org_id, status)
  - ix_fact_org_status ON facts (org_id, status)
  - ix_organization_created_by ON organizations (created_by)
  （shots(storyboard_id) 的 ix_shot_storyboard 已在 0001 建好。）

Revision ID: 0002_work_versions
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_work_versions"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _indexes(conn, table: str) -> set:
    return {i["name"] for i in sa.inspect(conn).get_indexes(table)}


def _create_index_if_missing(conn, name: str, table: str, cols, unique=False):
    if table in _tables(conn) and name not in _indexes(conn, table):
        op.create_index(name, table, cols, unique=unique)


def upgrade() -> None:
    conn = op.get_bind()

    # ---- work_versions 表 ----
    if "work_versions" not in _tables(conn):
        op.create_table(
            "work_versions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("work_id", sa.String(32),
                      sa.ForeignKey("works.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("title", sa.String(256), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("work_id", "version_no", name="uq_workversion_no"),
        )
    _create_index_if_missing(conn, "ix_workversion_work", "work_versions",
                             ["work_id"])

    # ---- 094 复合索引 ----
    _create_index_if_missing(conn, "ix_work_org_status", "works",
                             ["org_id", "status"])
    _create_index_if_missing(conn, "ix_fact_org_status", "facts",
                             ["org_id", "status"])
    _create_index_if_missing(conn, "ix_organization_created_by", "organizations",
                             ["created_by"])


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    for ix_name, table in (("ix_work_org_status", "works"),
                           ("ix_fact_org_status", "facts"),
                           ("ix_organization_created_by", "organizations"),
                           ("ix_workversion_work", "work_versions")):
        if table in existing and ix_name in _indexes(conn, table):
            op.drop_index(ix_name, table_name=table)
    if "work_versions" in existing:
        op.drop_table("work_versions")
