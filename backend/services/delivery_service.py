"""021：交付检查业务逻辑（从 app.py 迁移）。

路由层只做：参数接收 → 调 service → 返回结果。
所有"能不能交付"的业务规则收敛在这里，禁止在路由里散写。
"""
from __future__ import annotations

from typing import Literal, Optional


def _delivery_item(item_id: str, label: str, status: str, reason: str,
                   action: str = "", blocking: bool = True) -> dict:
    """001: 每个检查项显式标注是否为阻断项（blocking）。
    只有阻断项全部通过（done/na），才算可交付；建议项（suggest）不影响 ready。"""
    return {"id": item_id, "label": label, "status": status,
            "reason": reason, "action": action, "blocking": blocking}


def check_delivery(
    storyboard: Optional[dict] = None,
    product: Optional[dict] = None,
    facts: Optional[list] = None,
    feasibility: Optional[dict] = None,
    shoot_state: Optional[dict] = None,
    check_type: Literal["plan", "final"] = "plan",
    validate_fn=None,
) -> dict:
    """根据当前真实数据计算交付状态；不使用硬编码完成率。

    check_type="plan": 策划方案可交付 —— 产品/分镜/时长校验/风险审核为阻断项，
        事实库与拍摄进度仅为建议项。
    check_type="final": 最终成片可交付 —— 在 plan 基础上，拍摄进度与可行性
        也成为阻断项。

    validate_fn: 分镜校验函数（默认用 backend.validator.validate_dict，
    参数注入以便测试）。
    """
    if validate_fn is None:
        from ..validator import validate_dict as validate_fn

    is_final = (check_type or "plan") == "final"
    sb = storyboard or {}
    shots = sb.get("shots") or []
    facts = facts or []
    shoot_state = shoot_state or {}
    items = []

    # 1. 产品资料（阻断）
    has_product = bool(product and product.get("product_name"))
    items.append(_delivery_item(
        "product", "产品资料", "done" if has_product else "action",
        "产品名称已填写" if has_product else "尚未填写产品资料",
        "返回产品资料", blocking=True))

    # 2. 可信事实（建议项，不阻断）
    confirmed_facts = [f for f in facts
                       if f.get("status") == "confirmed" and f.get("allow_use") is True]
    items.append(_delivery_item(
        "facts", "可信事实",
        "done" if confirmed_facts else "suggest",
        f"存在 {len(confirmed_facts)} 条已确认且允许使用的事实" if confirmed_facts
        else "建议至少确认一条产品事实（需已确认且允许使用）",
        "检查事实库", blocking=False))

    # 3. 创意与分镜（阻断）
    items.append(_delivery_item(
        "storyboard", "创意与分镜", "done" if shots else "action",
        f"已生成 {len(shots)} 个镜头" if shots else "尚未生成分镜",
        "返回分镜编辑", blocking=True))

    # 4. 时长与校验（阻断，服务端 validate_dict 为准）
    sb_errors, _ = validate_fn(sb) if sb else (["缺少分镜"], None)
    items.append(_delivery_item(
        "duration", "时长与校验", "done" if shots and not sb_errors else "action",
        "分镜校验通过" if shots and not sb_errors else "；".join(sb_errors[:2]),
        "修正分镜", blocking=True))

    # 5. 风险审核（阻断：未运行或有未处理风险都不能放行）
    flags = sb.get("risk_flags") or []
    unresolved = [f for f in flags if not f.get("confirmed") or f.get("level") == "高"]
    if unresolved:
        risk_status, risk_reason = "action", f"有 {len(unresolved)} 条风险未完成人工确认"
    elif flags:
        risk_status, risk_reason = "done", "风险已记录并确认"
    else:
        risk_status, risk_reason = "action", "尚未运行风险检查，请先完成风险审核"
    items.append(_delivery_item(
        "risks", "风险审核", risk_status, risk_reason, "处理风险", blocking=True))

    # 6. 现场拍摄（plan: 建议项；final: 阻断项）
    shoot_shots = shoot_state.get("shots") or {}
    all_shoot_done = (bool(shots) and len(shoot_shots) >= len(shots)
                      and all(v.get("status") == "done" for v in shoot_shots.values()))
    items.append(_delivery_item(
        "shooting", "现场拍摄",
        "done" if all_shoot_done else ("suggest" if shots else "na"),
        "所有镜头已标记完成" if all_shoot_done else "拍摄状态未全部完成",
        "进入现场拍摄", blocking=is_final))

    # 7. 可行性检查（final: 阻断项；plan: 建议项）
    feas_results = (feasibility or {}).get("results") or []
    infeasible = [r for r in feas_results if r.get("status") == "不可执行"]
    if feas_results and not infeasible:
        feas_status, feas_reason = "done", f"已检查 {len(feas_results)} 个镜头，均无不可执行项"
    elif infeasible:
        feas_status, feas_reason = "action", f"有 {len(infeasible)} 个镜头被判定不可执行，请调整"
    else:
        feas_status, feas_reason = "suggest", "尚未运行可行性检查"
    items.append(_delivery_item(
        "feasibility", "可行性检查", feas_status, feas_reason,
        "运行可行性检查", blocking=bool(infeasible)))

    # 001: 只有阻断项全部通过才算可交付；suggest 不再视为通过
    blockers = [i for i in items if i["blocking"]]
    pending = [i["id"] for i in blockers if i["status"] not in ("done", "na")]
    ready = bool(blockers) and not pending
    return {
        "ready": ready,
        "check_type": "final" if is_final else "plan",
        "items": items,
        "blocking_total": len(blockers),
        "blocking_pending": pending,
    }


# TODO(021)：risk_service.check_risk()、feasibility_service.check() 待迁移。
# 当前仍在 app.py 内联，迁移时保持 API 行为不变。
