"""0019: AI 调用台账补齐组织归属（P1 修复）。

问题：`ai_calls` 原本没有 org_id，组织归属只能经
`task_id → ai_tasks.org_id` 推导；同步 AI 接口不建 ai_tasks 行，
task_id 恒为 NULL，导致用量仪表盘 / 调用台账 / 组织用量报表 /
每日调用配额四处全部漏计（实测 2178/2181 条调用无法归属）。

修复：新增可空列 `ai_calls.org_id` + 索引，网关于调用结束时写入
已校验的组织（匿名调用仍为 NULL）；统计侧优先用该列，存量行回退
task 关联，历史数据不丢。

幂等：本迁移先探测列是否已存在（启动期 ensure_columns 可能已补过），
已存在则跳过，避免存量库「先补列后打戳」时撞 duplicate column。

Revision ID: 0019_ai_call_org
Revises: 0018_review
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_ai_call_org"
down_revision: Union[str, None] = "0018_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    if not _has_column("ai_calls", "org_id"):
        op.add_column("ai_calls", sa.Column("org_id", sa.String(32),
                                            nullable=True))
    if not _has_index("ai_calls", "ix_aicall_org"):
        op.create_index("ix_aicall_org", "ai_calls", ["org_id"])


def downgrade() -> None:
    if _has_index("ai_calls", "ix_aicall_org"):
        op.drop_index("ix_aicall_org", table_name="ai_calls")
    if _has_column("ai_calls", "org_id"):
        op.drop_column("ai_calls", "org_id")
