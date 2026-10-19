"""129：模型能力注册表。调用前校验任务需求 vs 模型能力。

不匹配直接抛 AIFatalError（不浪费一次真实调用）。
db=None 时跳过校验（保持旧行为）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..ai_service import AIFatalError

logger = logging.getLogger(__name__)

# 任务对模型能力的硬需求。json_object=True 的任务必须要求 supports_json。
TASK_REQUIREMENTS: dict[str, dict] = {
    "ideas":       {"needs_json": True,  "needs_vision": False},
    "storyboard":  {"needs_json": True,  "needs_vision": False},
    "regen_shot":  {"needs_json": True,  "needs_vision": False},
    "feasibility": {"needs_json": True,  "needs_vision": False},
    "risk_check":  {"needs_json": True,  "needs_vision": False},
    "fact_extract": {"needs_json": True,  "needs_vision": False},
    "livestream_script": {"needs_json": True,  "needs_vision": False},
    "colloquial_rewrite": {"needs_json": True,  "needs_vision": False},
}


def get_capability(db, model: str) -> Optional[dict]:
    """取模型能力行；无 db 或无行时返回 None（跳过校验）。"""
    if db is None:
        return None
    try:
        from ..models import ModelCapability
        row = db.query(ModelCapability).filter_by(model=model).first()
        if row is None:
            return None
        return {"model": row.model, "max_tokens": row.max_tokens,
                "supports_vision": bool(row.supports_vision),
                "supports_json": bool(row.supports_json),
                "cost_tier": row.cost_tier}
    except Exception as e:
        logger.warning("读取 model_capabilities 失败 model=%s: %s", model, e)
        return None


def check_capability(db, model: str, task_type: str,
                     json_object: bool = False) -> None:
    """129：调用前能力校验。不满足 → AIFatalError，不发起调用。"""
    req = TASK_REQUIREMENTS.get(task_type, {})
    needs_json = bool(req.get("needs_json")) or json_object
    needs_vision = bool(req.get("needs_vision"))
    if not needs_json and not needs_vision:
        return
    cap = get_capability(db, model)
    if cap is None:
        return  # 无注册信息时不拦截（兼容旧行为）
    problems = []
    if needs_json and not cap["supports_json"]:
        problems.append("需要 JSON 结构化输出，但模型不支持")
    if needs_vision and not cap["supports_vision"]:
        problems.append("需要视觉理解，但模型不支持")
    if problems:
        raise AIFatalError(
            f"模型 {model} 不满足任务 {task_type} 的能力需求："
            + "；".join(problems)
            + "。请更换模型或更新 model_capabilities 注册表。")


def list_capabilities(db) -> list:
    """管理 API 用：列出全部已注册模型能力。"""
    from ..models import ModelCapability
    rows = db.query(ModelCapability).order_by(ModelCapability.model).all()
    return [{"model": r.model, "max_tokens": r.max_tokens,
             "supports_vision": bool(r.supports_vision),
             "supports_json": bool(r.supports_json),
             "cost_tier": r.cost_tier} for r in rows]


def set_capability(db, model: str, max_tokens: int = 0,
                   supports_vision: bool = False, supports_json: bool = True,
                   cost_tier: str = "standard") -> dict:
    """管理 API 用：新增/更新模型能力（管理员）。"""
    from ..models import ModelCapability
    if not model or not model.strip():
        raise ValueError("model 不能为空")
    model = model.strip()
    row = db.query(ModelCapability).filter_by(model=model).first()
    if row is None:
        row = ModelCapability(model=model)
        db.add(row)
    row.max_tokens = max(0, int(max_tokens or 0))
    row.supports_vision = bool(supports_vision)
    row.supports_json = bool(supports_json)
    row.cost_tier = (cost_tier or "standard").strip() or "standard"
    db.commit()
    logger.info("模型能力更新 model=%s vision=%s json=%s tier=%s",
                model, row.supports_vision, row.supports_json, row.cost_tier)
    return {"model": row.model, "max_tokens": row.max_tokens,
            "supports_vision": bool(row.supports_vision),
            "supports_json": bool(row.supports_json),
            "cost_tier": row.cost_tier}
