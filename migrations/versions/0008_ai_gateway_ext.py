"""0008: AI 网关扩展——任务选模型策略 + 模型能力注册表 + prompt 版本字段。

- task_model_policy：128，task_type → 主模型 + 兜底模型
- model_capabilities：129，模型能力注册表（调用前校验）
- ai_tasks.prompt_version：141-160，记录实际使用的 prompt 版本

Revision ID: 0008_ai_gateway_ext
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ai_gateway_ext"
down_revision: Union[str, None] = "0007_media"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 种子：与 DEFAULT_MODEL 一致，保证现有调用行为不变。
# fallback 初始为空（管理员按需配置）；能力值为种子占位，运维按实际更新。
_SEED_MODEL = "gpt-6-luna"
_SEED_POLICY = [
    ("ideas", _SEED_MODEL, None),        # 创意：强模型
    ("storyboard", _SEED_MODEL, None),   # 分镜：均衡
    ("regen_shot", _SEED_MODEL, None),
    ("feasibility", _SEED_MODEL, None),
    ("risk_check", _SEED_MODEL, None),   # 风险：精确
]


def upgrade() -> None:
    op.create_table(
        "task_model_policy",
        sa.Column("task_type", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("fallback_model", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "model_capabilities",
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("max_tokens", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("supports_vision", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("supports_json", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("cost_tier", sa.String(16), nullable=False,
                  server_default="standard"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("ai_tasks",
                  sa.Column("prompt_version", sa.String(32), nullable=True))

    # 种子数据
    for task_type, model, fallback in _SEED_POLICY:
        op.execute(
            sa.text(
                "INSERT INTO task_model_policy (task_type, model, fallback_model, updated_at) "
                "VALUES (:t, :m, :f, CURRENT_TIMESTAMP)"
            ).bindparams(t=task_type, m=model, f=fallback)
        )
    op.execute(
        sa.text(
            "INSERT INTO model_capabilities "
            "(model, max_tokens, supports_vision, supports_json, cost_tier, updated_at) "
            "VALUES (:m, 0, 0, 1, 'standard', CURRENT_TIMESTAMP)"
        ).bindparams(m=_SEED_MODEL)
    )


def downgrade() -> None:
    op.drop_column("ai_tasks", "prompt_version")
    op.drop_table("model_capabilities")
    op.drop_table("task_model_policy")
