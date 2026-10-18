"""0009: 企业管理与商业化（S10）——配额 / Webhook / 模板市场 / 计费。

- quotas（S10）：组织维度硬限额（ai_calls_daily / storage_mb / members）。
- webhooks + webhook_deliveries（S10）：组织事件订阅与投递记录（死信）。
- templates + template_versions（S10）：模板市场数据模型 + 版本快照。
- price_configs + billing_records（S10）：价格版本与对账单。
  注意：价格 seed 为 0 占位，上线前必须由运营配置真实价格。

Revision ID: 0009_enterprise
Replaces: 0008_ai_gateway_ext（并行组未提交文件；其 revision id 以文件为准）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_enterprise"
# 并行组（AI 网关扩展）的 revision；若其文件改名，此处需同步。
down_revision: Union[str, None] = "0008_ai_gateway_ext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # 补漏：Phase 5 网关组新增了 AIBudget 模型但未写 migration，
    # 此处幂等补建（若已存在则跳过）。
    if "ai_budgets" not in tables:
        op.create_table(
            "ai_budgets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("period", sa.String(7), nullable=False),
            sa.Column("monthly_limit_cents", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("used_cents", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("org_id", "period",
                                name="uq_budget_org_period"),
            sa.Index("ix_budget_org", "org_id"),
        )

    if "quotas" not in tables:
        op.create_table(
            "quotas",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("quota_type", sa.String(32), nullable=False),
            sa.Column("limit_value", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("org_id", "quota_type",
                                name="uq_quota_org_type"),
            sa.Index("ix_quota_org", "org_id"),
        )

    if "webhooks" not in tables:
        op.create_table(
            "webhooks",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("events_json", sa.Text(), nullable=False,
                      server_default="[]"),
            sa.Column("secret", sa.String(64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default="1"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_webhook_org", "org_id"),
        )

    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("webhook_id", sa.String(32),
                      sa.ForeignKey("webhooks.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("next_retry_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("delivered_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Index("ix_whdelivery_wh", "webhook_id"),
            sa.Index("ix_whdelivery_status", "status"),
        )

    if "templates" not in tables:
        op.create_table(
            "templates",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("category", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("content_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("is_public", sa.Boolean(), nullable=False,
                      server_default="0"),
            sa.Column("source", sa.String(32), nullable=False,
                      server_default="custom"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_template_org", "org_id"),
            sa.Index("ix_template_public", "is_public"),
        )

    if "template_versions" not in tables:
        op.create_table(
            "template_versions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("template_id", sa.String(32),
                      sa.ForeignKey("templates.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Index("ix_tversion_tpl", "template_id"),
        )

    if "price_configs" not in tables:
        op.create_table(
            "price_configs",
            sa.Column("id", sa.Integer(), primary_key=True,
                      autoincrement=True),
            sa.Column("item", sa.String(64), nullable=False),
            sa.Column("unit_price_cents", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("currency", sa.String(8), nullable=False,
                      server_default="CNY"),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("note", sa.String(256), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("item", "version",
                                name="uq_price_item_version"),
            sa.Index("ix_price_item", "item"),
        )

    if "billing_records" not in tables:
        op.create_table(
            "billing_records",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("org_id", sa.String(32),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("period", sa.String(7), nullable=False),
            sa.Column("usage_json", sa.Text(), nullable=False,
                      server_default="{}"),
            sa.Column("amount_cents", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("currency", sa.String(8), nullable=False,
                      server_default="CNY"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="draft"),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("org_id", "period",
                                name="uq_billing_org_period"),
            sa.Index("ix_billing_org", "org_id"),
        )


def downgrade() -> None:
    for t in ("billing_records", "price_configs", "template_versions",
              "templates", "webhook_deliveries", "webhooks", "quotas"):
        try:
            op.drop_table(t)
        except Exception:  # noqa: BLE001 - 幂等回滚
            pass
