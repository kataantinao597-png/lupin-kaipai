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


def _gateway_for_runner(task_id: str, payload: dict[str, Any], service=None):
    """runner 专用网关：带上 org_id + 独立 db 会话，使预算/用量生效。

    org 取自 payload["_org_id"]（API 层 S6.1 注入，已做成员校验）。
    返回 (gateway, db_session)，调用方必须在 finally 中 db.close()。
    无 _org_id 时行为与旧版一致（跳过预算），向后兼容。
    """
    from ..ai.gateway import AIGateway
    from ..auth.database import get_session_factory
    db = get_session_factory()()
    gw = AIGateway(service=service if service is not None else _get_server_ai(),
                   org_id=payload.get("_org_id"), db=db, task_id=task_id)
    return gw, db


def _resolve_payload_product_facts(payload: dict[str, Any],
                                     facts: list) -> tuple[list, dict | None]:
    """V3.0 B-4：异步任务 payload 带 product_id 时，解析其已确认事实并入。

    返回 (合并后的 facts, product 摘要或 None)。无 product_id 时原样返回。
    只带入 confirmed+allow_use+未过期的事实（get_product_grounding 保证）。
    """
    if not payload.get("product_id"):
        return facts, None
    org_id = payload.get("_org_id") or payload.get("org_id")
    from ..auth.database import get_session_factory
    from ..api.v1.products import get_product_grounding, merge_grounding_facts
    db = get_session_factory()()
    try:
        prod, pfacts = get_product_grounding(db, org_id, payload["product_id"])
        return merge_grounding_facts(facts, pfacts), prod
    finally:
        db.close()


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
    gw, _gw_db = _gateway_for_runner(task_id, payload)
    try:
        product = payload.get("product") or {}
        facts = payload.get("facts") or []
        # V3.0 B-4：产品档案的已确认事实并入 grounding
        facts, _prod_ref = _resolve_payload_product_facts(payload, facts)
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
    finally:
        _gw_db.close()


def run_storyboard(task_id: str, is_cancelled: Callable[[], bool],
                   payload: dict[str, Any]) -> dict[str, Any]:
    """034：分镜生成异步 runner。payload: {product, direction, facts, video_purpose?, available_resources?}。"""
    from ..ai.prompts import get_prompt_version
    from ..prompts import build_storyboard_messages, build_storyboard_v2_messages
    from ..validator import validate_dict

    if is_cancelled():
        _cancelled()
    gw, _gw_db = _gateway_for_runner(task_id, payload)
    try:
        product = payload.get("product") or {}
        direction = payload.get("direction") or ""
        facts = payload.get("facts") or []
        # V3.0 B-4：产品档案的已确认事实并入 grounding
        facts, _prod_ref = _resolve_payload_product_facts(payload, facts)
        _check_fact_refs(payload, facts)  # S6.1：gateway 调用前检查引用事实

        _purpose = (payload.get("video_purpose") or "").strip()
        _use_v2 = _purpose in {"brand", "sales", "intro", "record"}
        _v2_purpose = _purpose if _use_v2 else "intro"
        _resources = payload.get("available_resources") or []

        def make_messages(feedback):
            if _use_v2:
                return build_storyboard_v2_messages(
                    product, direction, _v2_purpose, _resources, facts, feedback)
            return build_storyboard_messages(product, direction, facts, feedback)

        sb, meta = gw.generate_with_retry(
            "storyboard", make_messages, validate_dict,
            model=payload.get("model") or None, temperature=0.3,
            prompt_version=get_prompt_version("storyboard_v2" if _use_v2 else "storyboard"))
        if is_cancelled():
            _cancelled()
        return {
            "storyboard": sb.model_dump() if hasattr(sb, "model_dump") else sb,
            "attempts": meta["attempts"],
            "usage": meta["usage"],
        }
    finally:
        _gw_db.close()


LIVESTREAM_PHASES = ("开场留人", "讲品", "互动", "逼单", "返场", "收尾")


def _validate_livestream(data: dict, duration=None):
    """校验店播场控脚本结构。返回 (errors, validated)。

    V3.1 P0-3：补全时间轴校验——phase 合法枚举、start<end、段间连续
    无重叠无空洞、覆盖 0→total_minutes、total 与请求时长一致、
    每段 script 非空。duration 为 None 时跳过"与请求时长一致"检查
    （兼容旧调用）。
    """
    errors = []
    if not isinstance(data, dict):
        return ["返回不是 JSON 对象"], None
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("[结构] 缺少 title")
    total = data.get("total_minutes")
    total_ok = isinstance(total, (int, float)) and total > 0
    if not total_ok:
        errors.append("[结构] total_minutes 非法（须为正数）")
    if duration is not None and total_ok and total != duration:
        errors.append(
            f"[时间轴] total_minutes({total}) 与请求时长({duration}) 不一致")
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        errors.append("segments 为空或不是数组")
    else:
        prev_end = 0
        for i, s in enumerate(segs):
            if not isinstance(s, dict):
                errors.append(f"segments[{i}] 不是对象")
                continue
            for k in ("phase", "start_minute", "end_minute", "script"):
                if k not in s:
                    errors.append(f"segments[{i}] 缺少字段 {k}")
            ph = s.get("phase")
            if ph not in LIVESTREAM_PHASES:
                errors.append(
                    f"segments[{i}] phase 非法: {ph!r}（允许 {list(LIVESTREAM_PHASES)}）")
            st, en = s.get("start_minute"), s.get("end_minute")
            if (not isinstance(st, (int, float))
                    or not isinstance(en, (int, float))
                    or st < 0 or en <= st):
                errors.append(f"segments[{i}] 起止时间非法: {st}-{en}")
                continue
            if st != prev_end:
                errors.append(
                    f"segments[{i}] 时间不连续：上一段结束于 {prev_end}，"
                    f"本段开始于 {st}")
            prev_end = en
            sc = s.get("script")
            if (not isinstance(sc, list)
                    or not any(isinstance(x, str) and x.strip() for x in sc)):
                errors.append(f"segments[{i}] script 为空")
        if total_ok and prev_end != total:
            errors.append(
                f"[时间轴] 未完整覆盖：最后一段结束于 {prev_end}，"
                f"总时长为 {total}")
    if errors:
        return errors, None
    return [], data


def run_livestream_script(task_id: str, is_cancelled: Callable[[], bool],
                          payload: dict[str, Any]) -> dict[str, Any]:
    """店播场控脚本异步 runner。payload: {product, facts, category, duration_minutes}。"""
    from ..ai.prompts import get_prompt_version
    from ..ai.prompts.livestream_v1 import build_livestream_messages

    if is_cancelled():
        _cancelled()
    gw, _gw_db = _gateway_for_runner(task_id, payload)
    try:
        product = payload.get("product") or {}
        facts = payload.get("facts") or []
        category = payload.get("category") or "水果"
        try:
            duration = int(payload.get("duration_minutes") or 120)
        except (TypeError, ValueError):
            duration = 120
        duration = max(30, min(duration, 480))
        _check_fact_refs(payload, facts)  # S6.1：gateway 调用前检查引用事实

        def make_messages(feedback):
            msgs = build_livestream_messages(product, facts, category, duration)
            if feedback:
                msgs.append({"role": "user", "content": feedback})
            return msgs

        # V3.1 P0-3：校验器带上请求时长做时间轴一致性检查；
        # 多次重试仍不合格 → generate_with_retry 抛 AIFatalError → 任务明确失败，
        # 残缺结果不会写入作品（前端 saveWork 只在 succeeded 时执行）
        script, meta = gw.generate_with_retry(
            "livestream_script", make_messages,
            lambda data: _validate_livestream(data, duration),
            model=payload.get("model") or None, temperature=0.7,
            prompt_version=get_prompt_version("livestream_script"))
        if is_cancelled():
            _cancelled()
        return {
            "livestream_script": script,
            "attempts": meta["attempts"],
            "usage": meta["usage"],
        }
    finally:
        _gw_db.close()


# 034：task_type → runner 映射
RUNNERS: dict[str, Callable] = {
    "ideas": run_ideas,
    "storyboard": run_storyboard,
    "livestream_script": run_livestream_script,
}
