"""0007: 媒体资产——对象存储元数据 + 上传意图 + 处理任务（S8.1/S8.2）。

- assets：媒体资产元数据（业务库只存元数据 + 对象 key）
- upload_intents：一次性上传授权（token 即主键，一次有效）
- media_tasks：媒体处理任务（元数据提取）
- shots：参考图版权/来源/授权标注字段（S8.3）

Revision ID: 0007_media
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_media"
down_revision: Union[str, None] = "0006_fact_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # ---- S8.1：assets ----
    if "assets" not in tables:
        op.create_table(
            "assets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("work_id", sa.String(32),
                      sa.ForeignKey("works.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("key", sa.String(512), nullable=False, unique=True),
            sa.Column("filename", sa.String(256), nullable=False,
                      server_default=""),
            sa.Column("mime", sa.String(128), nullable=False, server_default=""),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sha256", sa.String(64), nullable=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="uploading"),
            sa.Column("meta_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_asset_org_work", "org_id", "work_id"),
            sa.Index("ix_asset_status", "status"),
            sa.UniqueConstraint("key", name="uq_asset_key"),
        )

    # ---- S8.1：upload_intents ----
    if "upload_intents" not in tables:
        op.create_table(
            "upload_intents",
            sa.Column("token", sa.String(64), primary_key=True),
            sa.Column("org_id", sa.String(32), nullable=False),
            sa.Column("work_id", sa.String(32), nullable=False),
            sa.Column("key", sa.String(512), nullable=False),
            sa.Column("filename", sa.String(256), nullable=False,
                      server_default=""),
            sa.Column("mime", sa.String(128), nullable=False, server_default=""),
            sa.Column("expected_size", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("expected_sha256", sa.String(64), nullable=True),
            sa.Column("used", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_intent_org", "org_id"),
            sa.Index("ix_intent_expires", "expires_at"),
        )

    # ---- S8.2：media_tasks ----
    if "media_tasks" not in tables:
        op.create_table(
            "media_tasks",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("asset_id", sa.String(32),
                      sa.ForeignKey("assets.id", ondelete="CASCADE"),
                      nullable=False, unique=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="pending"),
            sa.Column("meta_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_mediatask_status", "status"),
            sa.UniqueConstraint("asset_id", name="uq_mediatask_asset"),
        )

    # ---- S8.3：shots 参考图版权/来源/授权标注 ----
    if "shots" in tables:
        cols = _columns(conn, "shots")
        if "ref_image_copyright" not in cols:
            op.add_column("shots",
                          sa.Column("ref_image_copyright", sa.String(256),
                                    nullable=True))
        if "ref_image_source" not in cols:
            op.add_column("shots",
                          sa.Column("ref_image_source", sa.String(512),
                                    nullable=True))
        if "ref_image_license" not in cols:
            op.add_column("shots",
                          sa.Column("ref_image_license", sa.String(128),
                                    nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)
    if "media_tasks" in tables:
        op.drop_table("media_tasks")
    if "upload_intents" in tables:
        op.drop_table("upload_intents")
    if "assets" in tables:
        op.drop_table("assets")
    if "shots" in tables:
        cols = _columns(conn, "shots")
        for col in ("ref_image_license", "ref_image_source",
                    "ref_image_copyright"):
            if col in cols:
                op.drop_column("shots", col)
