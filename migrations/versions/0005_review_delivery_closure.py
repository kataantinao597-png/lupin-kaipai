"""0005: 审核版本绑定 + 交付门禁配置化 + 风险发现追踪（S6.2-S6.5）。

- reviews 表新增 rule_version / model_version / prompt_version（S6.3）。
- delivery_gate_configs（S6.5）：门禁配置，种子数据与旧硬编码行为一致。
- waivers（S6.5）：阻断项豁免记录。
- risk_findings（S6.4）：风险发现追踪。

Revision ID: 0005_review_delivery_closure
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_review_delivery_closure"
down_revision: Union[str, None] = "0004_ai_observability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


# S6.5：默认门禁配置——与 delivery_service 旧硬编码行为完全一致。
# (gate_type, check_id, label, is_blocking, is_waivable,
#  conditional_blocking, sort_order)
_DEFAULT_GATES = [
    # plan：策划方案可交付
    ("plan", "product", "产品资料", True, False, False, 10),
    ("plan", "facts", "可信事实", False, False, False, 20),
    ("plan", "storyboard", "创意与分镜", True, False, False, 30),
    ("plan", "duration", "时长与校验", True, False, False, 40),
    ("plan", "risks", "风险审核", True, False, False, 50),
    ("plan", "shooting", "现场拍摄", False, True, False, 60),
    ("plan", "feasibility", "可行性检查", False, True, True, 70),
    # production：生产交付（与 final 同标准）
    ("production", "product", "产品资料", True, False, False, 10),
    ("production", "facts", "可信事实", False, False, False, 20),
    ("production", "storyboard", "创意与分镜", True, False, False, 30),
    ("production", "duration", "时长与校验", True, False, False, 40),
    ("production", "risks", "风险审核", True, False, False, 50),
    ("production", "shooting", "现场拍摄", True, True, False, 60),
    ("production", "feasibility", "可行性检查", False, True, True, 70),
    # final：最终成片可交付
    ("final", "product", "产品资料", True, False, False, 10),
    ("final", "facts", "可信事实", False, False, False, 20),
    ("final", "storyboard", "创意与分镜", True, False, False, 30),
    ("final", "duration", "时长与校验", True, False, False, 40),
    ("final", "risks", "风险审核", True, False, False, 50),
    ("final", "shooting", "现场拍摄", True, True, False, 60),
    ("final", "feasibility", "可行性检查", False, True, True, 70),
]


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # ---- S6.3：reviews 新增版本绑定字段 ----
    if "reviews" in tables:
        cols = _columns(conn, "reviews")
        if "rule_version" not in cols:
            op.add_column("reviews",
                          sa.Column("rule_version", sa.String(64),
                                    nullable=False, server_default=""))
        if "model_version" not in cols:
            op.add_column("reviews",
                          sa.Column("model_version", sa.String(128),
                                    nullable=False, server_default=""))
        if "prompt_version" not in cols:
            op.add_column("reviews",
                          sa.Column("prompt_version", sa.String(64),
                                    nullable=False, server_default=""))

    # ---- S6.5：delivery_gate_configs ----
    if "delivery_gate_configs" not in tables:
        op.create_table(
            "delivery_gate_configs",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("gate_type", sa.String(16), nullable=False),
            sa.Column("check_id", sa.String(32), nullable=False),
            sa.Column("label", sa.String(128), nullable=False, server_default=""),
            sa.Column("is_blocking", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("is_waivable", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("conditional_blocking", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("required_permission", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("updated_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("gate_type", "check_id", name="uq_gate_check"),
        )
        op.create_index("ix_gate_type", "delivery_gate_configs", ["gate_type"])

        # 种子数据：与旧硬编码一致
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for (gate_type, check_id, label, blocking, waivable,
             cond_blocking, order) in _DEFAULT_GATES:
            op.execute(
                sa.text(
                    "INSERT INTO delivery_gate_configs "
                    "(id, gate_type, check_id, label, is_blocking, is_waivable,"
                    " conditional_blocking, required_permission, sort_order,"
                    " created_at, updated_at) "
                    "VALUES (:id, :gate_type, :check_id, :label, :is_blocking,"
                    " :is_waivable, :conditional_blocking, '', :sort_order,"
                    " :now, :now)"
                ).bindparams(
                    id=uuid.uuid4().hex[:32], gate_type=gate_type,
                    check_id=check_id, label=label, is_blocking=blocking,
                    is_waivable=waivable, conditional_blocking=cond_blocking,
                    sort_order=order, now=now,
                )
            )

    # ---- S6.5：waivers ----
    if "waivers" not in tables:
        op.create_table(
            "waivers",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("work_id", sa.String(32),
                      sa.ForeignKey("works.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("gate_type", sa.String(16), nullable=False),
            sa.Column("check_id", sa.String(32), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("approved_by", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_waiver_work", "waivers", ["work_id"])
        op.create_index("ix_waiver_org", "waivers", ["org_id"])

    # ---- S6.4：risk_findings ----
    if "risk_findings" not in tables:
        op.create_table(
            "risk_findings",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("review_id", sa.String(32),
                      sa.ForeignKey("reviews.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("work_id", sa.String(32),
                      sa.ForeignKey("works.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("shot_id", sa.String(64), nullable=True),
            sa.Column("shot_no", sa.Integer(), nullable=True),
            sa.Column("text_fragment", sa.Text(), nullable=True),
            sa.Column("severity", sa.String(16), nullable=False,
                      server_default="medium"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="open"),
            sa.Column("source", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("resolved_by", sa.String(64), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_finding_review", "risk_findings", ["review_id"])
        op.create_index("ix_finding_work", "risk_findings", ["work_id"])
        op.create_index("ix_finding_status", "risk_findings", ["status"])


def downgrade() -> None:
    conn = op.get_bind()
    if "risk_findings" in _tables(conn):
        op.drop_table("risk_findings")
    if "waivers" in _tables(conn):
        op.drop_table("waivers")
    if "delivery_gate_configs" in _tables(conn):
        op.drop_table("delivery_gate_configs")
    if "reviews" in _tables(conn):
        cols = _columns(conn, "reviews")
        for col in ("rule_version", "model_version", "prompt_version"):
            if col in cols:
                op.drop_column("reviews", col)
