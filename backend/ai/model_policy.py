"""128：任务选模型策略。

解析顺序：调用方显式指定 model → task_model_policy[task_type] → DEFAULT_MODEL。
db=None（无数据库上下文，如单机脚本）时直接回退到 DEFAULT_MODEL，
保证行为与策略种子一致。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_policy(db, task_type: str) -> Optional[dict]:
    """取某任务的策略行；无 db 或无行时返回 None。"""
    if db is None:
        return None
    try:
        from ..models import TaskModelPolicy
        row = db.query(TaskModelPolicy).filter_by(task_type=task_type).first()
        if row is None:
            return None
        return {"task_type": row.task_type, "model": row.model,
                "fallback_model": row.fallback_model}
    except Exception as e:
        logger.warning("读取 task_model_policy 失败 task=%s: %s", task_type, e)
        return None


def resolve_model(db, task_type: str,
                  explicit_model: Optional[str] = None) -> str:
    """128：解析任务实际使用的模型。

    显式指定的模型优先（122 禁止静默回退：用户指定的就用指定的）；
    否则查策略表；都没有则用 DEFAULT_MODEL。

    注意：本函数不做白名单校验（test_policy_explicit_wins 与 122 锁定
    了“显式指定即用”语义，网关/策略层改了会反向破坏）。用户传入的
    model 白名单校验在 HTTP 边界做（validate_explicit_model），
    内部调用（策略表模型、兜底模型、测试 fake）不受其约束。
    """
    if explicit_model:
        return explicit_model
    pol = get_policy(db, task_type)
    if pol and pol["model"]:
        return pol["model"]
    from ..ai_service import DEFAULT_MODEL
    return DEFAULT_MODEL


def list_allowed_models(db) -> set[str]:
    """B-08：用户可显式指定的模型白名单。

    = 能力注册表（model_capabilities，管理员经 PUT /model-capabilities
    维护）∪ 策略表主/兜底模型 ∪ DEFAULT_MODEL。db=None 时只认
    DEFAULT_MODEL。注册表为空的新库也不放空——至少 DEFAULT 可用。
    """
    from ..ai_service import DEFAULT_MODEL
    allowed = {DEFAULT_MODEL}
    if db is None:
        return allowed
    try:
        from ..models import ModelCapability, TaskModelPolicy
        for (m,) in db.query(ModelCapability.model).all():
            if m:
                allowed.add(m)
        for row in db.query(TaskModelPolicy.model,
                            TaskModelPolicy.fallback_model).all():
            for m in row:
                if m:
                    allowed.add(m)
    except Exception as e:
        logger.warning("读取可用模型白名单失败: %s", e)
    return allowed


def validate_explicit_model(db, model: Optional[str]) -> None:
    """B-08：用户传入的 model 非空时必须在白名单内，否则 ValueError。

    None/空串 = 不指定，走服务端默认，不校验。调用方（HTTP 层）把
    ValueError 转 400。白名单外的模型此前会直达 provider：不存在的
    模型报 400 失败、高价模型绕过成本预期。
    """
    if not model:
        return
    allowed = list_allowed_models(db)
    if model not in allowed:
        raise ValueError(
            f"不支持的模型: {model}（可用模型: "
            f"{', '.join(sorted(allowed))}；如需新增请管理员在模型能力"
            "注册表登记）")


def resolve_fallback(db, task_type: str) -> Optional[str]:
    """130：取某任务的兜底模型（故障切换用）。无则返回 None。"""
    pol = get_policy(db, task_type)
    if pol:
        return pol["fallback_model"]
    return None


def list_policies(db) -> list:
    """管理 API 用：列出全部策略。"""
    from ..models import TaskModelPolicy
    rows = db.query(TaskModelPolicy).order_by(TaskModelPolicy.task_type).all()
    return [{"task_type": r.task_type, "model": r.model,
             "fallback_model": r.fallback_model} for r in rows]


def set_policy(db, task_type: str, model: str,
               fallback_model: Optional[str] = None) -> dict:
    """管理 API 用：新增/更新某任务的策略（管理员）。"""
    from ..models import TaskModelPolicy, AI_TASK_TYPES
    if task_type not in AI_TASK_TYPES:
        raise ValueError(f"未知 task_type: {task_type}（允许: {AI_TASK_TYPES}）")
    if not model or not model.strip():
        raise ValueError("model 不能为空")
    row = db.query(TaskModelPolicy).filter_by(task_type=task_type).first()
    if row is None:
        row = TaskModelPolicy(task_type=task_type, model=model.strip(),
                              fallback_model=(fallback_model or None))
        db.add(row)
    else:
        row.model = model.strip()
        row.fallback_model = fallback_model or None
    db.commit()
    logger.info("任务模型策略更新 task=%s model=%s fallback=%s",
                task_type, row.model, row.fallback_model)
    return {"task_type": row.task_type, "model": row.model,
            "fallback_model": row.fallback_model}
