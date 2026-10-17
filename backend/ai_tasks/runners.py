"""AI 任务执行器（034/035）：把同步 AI 逻辑包装成 runner(task_id, is_cancelled)。

- runner 只用服务端配置的 AI（环境变量）；用户请求头自带 Key 的
  不允许走异步（Key 不能落库），API 层直接拒绝。
- 034：payload 结构与同步接口的请求体一致，便于复用校验逻辑。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _get_server_ai():
    """取服务端配置的 AIService（环境变量）。未配置抛 RuntimeError。"""
    from ..ai_service import AIService, AIConfigError
    try:
        return AIService()
    except AIConfigError as e:
        raise RuntimeError(f"服务端 AI 未配置，无法执行异步任务：{e}")


def _cancelled():
    from .queue import _TaskCancelled
    raise _TaskCancelled()


def run_ideas(task_id: str, is_cancelled: Callable[[], bool],
              payload: dict[str, Any]) -> dict[str, Any]:
    """034：创意生成异步 runner。payload: {product, facts}。"""
    from ..ai_service import DEFAULT_MODEL
    from ..api.v1.ideas import _validate_ideas
    from ..prompts import build_idea_messages

    if is_cancelled():
        _cancelled()
    svc = _get_server_ai()
    product = payload.get("product") or {}
    facts = payload.get("facts") or []

    def make_messages(feedback):
        msgs = build_idea_messages(product, facts)
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    ideas, meta = svc.generate_with_retry(
        make_messages, _validate_ideas,
        model=payload.get("model") or DEFAULT_MODEL, temperature=0.7)
    if is_cancelled():
        _cancelled()
    return {
        "ideas": [i.model_dump() for i in ideas],
        "attempts": meta["attempts"],
        "usage": meta["usage"],
    }


def run_storyboard(task_id: str, is_cancelled: Callable[[], bool],
                   payload: dict[str, Any]) -> dict[str, Any]:
    """034：分镜生成异步 runner。payload: {product, direction, facts}。"""
    from ..ai_service import DEFAULT_MODEL
    from ..prompts import build_storyboard_messages
    from ..validator import validate_dict

    if is_cancelled():
        _cancelled()
    svc = _get_server_ai()
    product = payload.get("product") or {}
    direction = payload.get("direction") or ""
    facts = payload.get("facts") or []

    def make_messages(feedback):
        return build_storyboard_messages(product, direction, facts, feedback)

    sb, meta = svc.generate_with_retry(
        make_messages, validate_dict,
        model=payload.get("model") or DEFAULT_MODEL, temperature=0.3)
    if is_cancelled():
        _cancelled()
    return {
        "storyboard": sb.model_dump() if hasattr(sb, "model_dump") else sb,
        "attempts": meta["attempts"],
        "usage": meta["usage"],
    }


# 034：task_type → runner 映射
RUNNERS: dict[str, Callable] = {
    "ideas": run_ideas,
    "storyboard": run_storyboard,
}
