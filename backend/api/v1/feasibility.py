"""022：可行性检查路由（从 app.py 拆分）。

- POST /api/feasibility-check  规则预检 + 模型批量分析

模型失败时降级为纯规则结果（model_completed=False），不抛 502。
TODO(021)：合并逻辑迁 services/feasibility_service.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError, DEFAULT_MODEL
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...core.deps import check_rate_limit, get_ai_gateway, get_request_model
from ...prompts import build_feasibility_messages
from ...schemas import FeasibilityReport
from ...validator import rule_feasibility_scan, validate_dict

router = APIRouter(tags=["feasibility"])


class FeasibilityCheckRequest(BaseModel):
    storyboard: dict


def _validate_feasibility(data: dict, shot_numbers: list):
    """校验模型返回的可行性报告结构。"""
    errors = []
    try:
        report = FeasibilityReport.model_validate(data)
    except Exception as e:
        return [f"[结构] 可行性报告解析失败: {e}"], None
    got = {r.shot_number for r in report.results}
    want = set(shot_numbers)
    if got != want:
        errors.append(f"[规则] 返回的镜头号 {sorted(got)} 与实际 {sorted(want)} 不一致")
    for r in report.results:
        if r.status in ("建议调整", "不可执行") and r.alternative is None:
            errors.append(f"[规则] 镜头 {r.shot_number} 状态为{r.status}但未提供替代方案")
    if errors:
        return errors, None
    return [], report


@router.post("/api/feasibility-check")
def check_feasibility(
    req: FeasibilityCheckRequest,
    request: Request,
    gw=Depends(get_ai_gateway),
):
    """智能拍摄可行性检查：规则预检 + 模型批量分析（一次请求覆盖所有镜头）。"""
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400, detail="分镜未通过校验，无法检查：" + "; ".join(errors[:3]))
    sb_dict = sb.model_dump()
    shot_numbers = [s.shot_number for s in sb.shots]

    # 1. 规则层预检（确定性、零成本）
    rule_findings = {num: (status, reason, constraints)
                     for num, status, reason, constraints in rule_feasibility_scan(sb)}

    # 2. 模型层批量分析（一次请求）
    check_rate_limit(request)
    _model = get_request_model(request)

    def make_messages(feedback):
        return build_feasibility_messages(sb_dict, feedback)

    try:
        report, meta = gw.generate_with_retry(
            "feasibility", make_messages,
            lambda d: _validate_feasibility(d, shot_numbers),
            model=_model or DEFAULT_MODEL, temperature=0.3)
    except BudgetExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except TaskLimitExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        # 模型失败：只返回规则结果，明确标记未完成
        results = []
        for num in shot_numbers:
            if num in rule_findings:
                status, reason, constraints = rule_findings[num]
                results.append({"shot_number": num, "status": status, "reason": reason,
                                "constraints_involved": constraints, "source": "规则",
                                "alternative": None})
            else:
                results.append({"shot_number": num, "status": "需要人工确认",
                                "reason": f"模型检查未完成（{e}），请人工判断",
                                "constraints_involved": [], "source": "规则", "alternative": None})
        return {"results": results, "model_completed": False,
                "notice": "模型检查未完成，仅显示规则预检结果"}

    # 3. 合并：规则命中的以规则为准（更严格），其余用模型结果
    merged = []
    model_map = {r.shot_number: r for r in report.results}
    for num in shot_numbers:
        if num in rule_findings:
            status, reason, constraints = rule_findings[num]
            m = model_map.get(num)
            merged.append({"shot_number": num, "status": status, "reason": reason,
                           "constraints_involved": constraints, "source": "规则",
                           "alternative": m.alternative.model_dump() if m and m.alternative else None})
        else:
            m = model_map[num]
            merged.append({"shot_number": num, "status": m.status, "reason": m.reason,
                           "constraints_involved": m.constraints_involved, "source": "模型",
                           "alternative": m.alternative.model_dump() if m.alternative else None})
    return {"results": merged, "model_completed": True, "attempts": meta["attempts"]}
