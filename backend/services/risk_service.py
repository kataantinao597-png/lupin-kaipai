"""021：风险审核业务逻辑（从 app.py 迁移）。

两层检查：
1. AI 层：risk.risk_check_storyboard（模型识别风险）
2. 规则层：risk.check_fact_grounding（确定性事实依据检查，零成本）

144：AI 失败/超时/解析失败不得被识别为"已通过"——异常直接上抛，
由路由层转为 502，前端明确展示失败而非静默放行。
"""
from __future__ import annotations

from typing import Optional

from ..risk import check_fact_grounding, risk_check_storyboard


def check_storyboard_risk(
    ai_service,
    storyboard: dict,
    evidence_materials: str = "",
    selling_points: Optional[list] = None,
    product_info: str = "",
    facts: Optional[list] = None,
    model: Optional[str] = None,
) -> dict:
    """执行完整的风险审核，返回 result（含 flags 列表）。

    ai_service: AIGateway 实例（由 core/deps.get_ai_gateway 注入；兼容 AIService）
    model: 实际使用的模型名（012：必须显式传递，禁止静默回退）
    """
    selling_points = selling_points or []
    facts = facts or []

    # 1. AI 层风险识别（异常上抛，不吞）
    result = risk_check_storyboard(
        ai_service, storyboard,
        evidence=evidence_materials,
        input_claims=selling_points,
        product_info=product_info,
        model=model,
    )

    # 2. V5+ 事实 grounding 规则层（确定性，不消耗 API）
    try:
        grounding_flags = check_fact_grounding(storyboard, facts)
        if grounding_flags:
            result["flags"] = result.get("flags", []) + [
                {"text": g["text"], "level": g["level"], "reason": g["reason"],
                 "shot_number": g["shot_number"], "source": "事实依据检查"}
                for g in grounding_flags
            ]
    except Exception:
        # 规则层异常不阻断 AI 结果（与原 app.py 行为一致）
        pass

    return result
