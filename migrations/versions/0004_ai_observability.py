"""0004: AI 可观测性表 + ai_tasks.priority（131/132/137）。

- ai_calls（131）：AI 调用台账（token、费用、状态、延迟、缓存标记）。
- model_prices（132）：模型价格表（每 1k token 输入/输出价 + 版本）。
- ai_tasks.priority（137）：high/low 优先级，队列优先执行 high。

Revision ID: 0004_ai_observability
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ai_observability"
down_revision: Union[str, None] = "0003_locks_reviews_aitasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _indexes(conn, table: str) -> set:
    return {i["name"] for i in sa.inspect(conn).get_indexes(table)}


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    # ---- 131：ai_calls ----
    if "ai_calls" not in tables:
        op.create_table(
            "ai_calls",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("task_id", sa.String(32), nullable=True),
            sa.Column("task_type", sa.String(32), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("cost_cents", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="success"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("cached", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
        )
    for name, cols in (("ix_aicall_task_type", ["task_type"]),
                       ("ix_aicall_model", ["model"]),
                       ("ix_aicall_created", ["created_at"]),
                       ("ix_aicall_task", ["task_id"])):
        if name not in _indexes(conn, "ai_calls"):
            op.create_index(name, "ai_calls", cols)

    # ---- 132：model_prices ----
    if "model_prices" not in tables:
        op.create_table(
            "model_prices",
            sa.Column("id", sa.Integer(), primary_key=True,
                      autoincrement=True),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("input_price_per_1k", sa.Float(), nullable=False),
            sa.Column("output_price_per_1k", sa.Float(), nullable=False),
            sa.Column("currency", sa.String(8), nullable=False,
                      server_default="CNY"),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.UniqueConstraint("model", "version",
                                name="uq_modelprice_model_version"),
        )
    if "ix_modelprice_model" not in _indexes(conn, "model_prices"):
        op.create_index("ix_modelprice_model", "model_prices", ["model"])

    # ---- 137：ai_tasks.priority ----
    if "ai_tasks" in tables and "priority" not in _columns(conn, "ai_tasks"):
        op.add_column("ai_tasks",
                      sa.Column("priority", sa.String(8), nullable=False,
                                server_default="high"))


def downgrade() -> None:
    conn = op.get_bind()
    if "ai_calls" in _tables(conn):
        op.drop_table("ai_calls")
    if "model_prices" in _tables(conn):
        op.drop_table("model_prices")
    if "priority" in _columns(conn, "ai_tasks"):
        op.drop_column("ai_tasks", "priority")
