"""0001: 初始 schema（082/083/084/085/086/088）

- 包含全部 10 张表的新版结构：users / refresh_tokens / password_resets /
  organizations / memberships / invitations / works（+status/deleted_at/version）/
  storyboards / shots / facts。
- 幂等保护：用 inspector 检查已存在的表/列/索引。
  - 全新库：建出全部表（含 086 外键、088 version 字段）。
  - Phase 2 老 dev 库（create_all 建的旧表）：跳过建表，只给 works 补
    status / deleted_at / version 三列（带 server_default，老数据自动回填）。
    注意：老表缺少 DB 级外键约束（旧模型本来就没有），如需外键请重建库。
- downgrade：删除本 revision 引入的表（storyboards/shots/facts）与
  works 新增三列；Phase 2 之前已存在的表保留，避免误删业务数据。

Revision ID: 0001_initial_schema
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _is_mock(conn) -> bool:
    # alembic upgrade --sql（offline）用 MockConnection，只能打 SQL 不能 inspect；
    # 此时按全新库处理（DBA 拿 SQL 脚本的场景本就是建新库）。
    return type(conn).__name__ == "MockConnection"


def _tables(conn) -> set:
    if _is_mock(conn):
        return set()
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    if _is_mock(conn):
        return set()
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _indexes(conn, table: str) -> set:
    if _is_mock(conn):
        return set()
    return {i["name"] for i in sa.inspect(conn).get_indexes(table)}


def _create_index_if_missing(conn, name: str, table: str, cols, unique=False):
    if _is_mock(conn) or name not in _indexes(conn, table):
        op.create_index(name, table, cols, unique=unique)


# ---- Phase 2 已有表的完整新版定义（086 外键 + 088 version） ----
def _create_users():
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )


def _create_refresh_tokens():
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )


def _create_password_resets():
    op.create_table(
        "password_resets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("token_hash", name="uq_password_resets_token_hash"),
    )


def _create_organizations():
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_memberships():
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
    )


def _create_invitations():
    op.create_table(
        "invitations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("code", name="uq_invitations_code"),
    )


def _create_works():
    op.create_table(
        "works",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("data_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_storyboards():
    op.create_table(
        "storyboards",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_shots():
    op.create_table(
        "shots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("storyboard_id", sa.String(32),
                  sa.ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shot_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("shot_id_stable", sa.String(64), nullable=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("storyboard_id", "shot_id_stable",
                            name="uq_shot_storyboard_stable"),
    )


def _create_facts():
    op.create_table(
        "facts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_type", sa.String(32), nullable=False,
                  server_default="human_statement"),
        sa.Column("source_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("allow_use", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("verified_by", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_work_versions():
    # 091：兄弟组 API 已引用，此处补建表
    op.create_table(
        "work_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32),
                  sa.ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "version_no", name="uq_workversion_no"),
    )


_LEGACY_CREATORS = {
    "users": _create_users,
    "refresh_tokens": _create_refresh_tokens,
    "password_resets": _create_password_resets,
    "organizations": _create_organizations,
    "memberships": _create_memberships,
    "invitations": _create_invitations,
    "works": _create_works,
}
_NEW_TABLES = {
    "storyboards": _create_storyboards,
    "shots": _create_shots,
    "facts": _create_facts,
    "work_versions": _create_work_versions,
}

# Phase 2 老 works 表缺的三列（083/088），带 server_default 回填老数据
_WORKS_NEW_COLUMNS = [
    ("status", sa.String(16), "draft"),
    ("deleted_at", sa.DateTime(timezone=True), None),
    ("version", sa.Integer(), "1"),
]

_INDEXES = [
    # (name, table, columns, unique)
    ("ix_users_email", "users", ["email"], True),
    ("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], False),
    ("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], True),
    ("ix_password_resets_user_id", "password_resets", ["user_id"], False),
    ("ix_password_resets_token_hash", "password_resets", ["token_hash"], True),
    ("ix_password_resets_user_created", "password_resets",
     ["user_id", "created_at"], False),
    ("ix_membership_org", "memberships", ["org_id"], False),
    ("ix_membership_user", "memberships", ["user_id"], False),
    ("ix_invitation_code", "invitations", ["code"], True),
    ("ix_invitation_org", "invitations", ["org_id"], False),
    ("ix_work_org", "works", ["org_id"], False),
    ("ix_work_owner", "works", ["owner_id"], False),
    ("ix_work_status", "works", ["status"], False),
    ("ix_storyboard_work", "storyboards", ["work_id"], False),
    ("ix_shot_storyboard", "shots", ["storyboard_id"], False),
    ("ix_shot_stable", "shots", ["shot_id_stable"], False),
    ("ix_fact_org", "facts", ["org_id"], False),
    ("ix_fact_product", "facts", ["product_id"], False),
    ("ix_fact_status", "facts", ["status"], False),
    ("ix_workversion_work", "work_versions", ["work_id"], False),
]


def upgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    ensured: set[str] = set()

    # Phase 2 老表：没有就按新版建，有就只补 works 新增列
    for name, creator in _LEGACY_CREATORS.items():
        if name not in existing:
            creator()
        elif name == "works":
            cols = _columns(conn, "works")
            for col_name, col_type, server_default in _WORKS_NEW_COLUMNS:
                if col_name not in cols:
                    op.add_column(
                        "works",
                        sa.Column(col_name, col_type, nullable=True,
                                  server_default=server_default),
                    )
            # 回填后再收紧非空（status/version 老数据已由 server_default 填好）
            with op.batch_alter_table("works") as batch:
                batch.alter_column("status", nullable=False)
                batch.alter_column("version", nullable=False)
        ensured.add(name)

    # Phase 3 新表
    for name, creator in _NEW_TABLES.items():
        if name not in existing:
            creator()
        ensured.add(name)

    # 索引（mock/offline 下 ensured 即全量表）
    for ix_name, table, cols, unique in _INDEXES:
        if table in ensured:
            _create_index_if_missing(conn, ix_name, table, cols, unique)


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)

    # 删除本 revision 引入的新表
    for name in ("shots", "storyboards", "facts", "work_versions"):
        if name in existing:
            op.drop_table(name)

    # 删除本 revision 给 works 加的三列（SQLite 用 batch 模式重建表；
    # 必须先删掉这些列上的索引，否则重建时会因列不存在而失败）
    if "works" in existing:
        cols = _columns(conn, "works")
        drop = [c for c in ("status", "deleted_at", "version") if c in cols]
        if drop:
            if "ix_work_status" in _indexes(conn, "works"):
                op.drop_index("ix_work_status", table_name="works")
            with op.batch_alter_table("works") as batch:
                for c in drop:
                    batch.drop_column(c)

    # Phase 2 之前已存在的表保留，不误删业务数据
