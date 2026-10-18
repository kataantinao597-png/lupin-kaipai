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


def _check_fact_refs(payload: dict[str, Any], facts: list) -> None:
    """S6.1：在 gateway 调用前检查引用事实状态。

    expired/revoked/未确认/不允许使用的事实不得被新生成内容引用。
    无 org 上下文或无事实 ID 时跳过（MVP 旧链路）；检查失败抛错，
    任务直接失败，不静默使用不可用事实。
    """
    org_id = payload.get("_org_id")
    if not org_id or not facts:
        return
    from ..auth.database import get_session_factory
    from ..facts.guard import assert_facts_usable, UnusableFactError
    db = get_session_factory()()
    try:
        assert_facts_usable(db, org_id, facts)
    except UnusableFactError as e:
        raise RuntimeError(f"事实引用检查失败，任务拒绝执行：{e}")
    finally:
        db.close()


def run_ideas(task_id: str, is_cancelled: Callable[[], bool],
              payload: dict[str, Any]) -> dict[str, Any]:
    """034：创意生成异步 runner。payload: {product, facts}。"""
    from ..ai.prompts import get_prompt_version
    from ..api.v1.ideas import _validate_ideas
    from ..prompts import build_idea_messages

    if is_cancelled():
        _cancelled()
    from ..ai.gateway import AIGateway
    gw = AIGateway(service=_get_server_ai(), task_id=task_id)
    product = payload.get("product") or {}
    facts = payload.get("facts") or []
    _check_fact_refs(payload, facts)  # S6.1：gateway 调用前检查引用事实

    def make_messages(feedback):
        msgs = build_idea_messages(product, facts)
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    ideas, meta = gw.generate_with_retry(
        "ideas", make_messages, _validate_ideas,
        model=payload.get("model") or None, temperature=0.7,
        prompt_version=get_prompt_version("ideas"))
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
    from ..ai.prompts import get_prompt_version
    from ..prompts import build_storyboard_messages
    from ..validator import validate_dict

    if is_cancelled():
        _cancelled()
    from ..ai.gateway import AIGateway
    gw = AIGateway(service=_get_server_ai(), task_id=task_id)
    product = payload.get("product") or {}
    direction = payload.get("direction") or ""
    facts = payload.get("facts") or []
    _check_fact_refs(payload, facts)  # S6.1：gateway 调用前检查引用事实

    def make_messages(feedback):
        return build_storyboard_messages(product, direction, facts, feedback)

    sb, meta = gw.generate_with_retry(
        "storyboard", make_messages, validate_dict,
        model=payload.get("model") or None, temperature=0.3,
        prompt_version=get_prompt_version("storyboard"))
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
