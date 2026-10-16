"""鲁品开拍 - FastAPI 后端入口。

安全：API Key 只在启动时从配置现读一次、放内存；绝不返回给前端、不写日志。
"""
import json
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import tempfile
from pydantic import BaseModel, Field

from .ai_service import AIService, AIConfigError, AIFatalError, AIRetryableError, DEFAULT_MODEL
from .schemas import Idea, Storyboard
from .validator import validate_dict
from .prompts import build_idea_messages, build_storyboard_messages, build_regen_shot_messages
from .schemas import Shot
from .risk import risk_check_storyboard, check_fact_grounding
from .exporter import export_docx, export_checklist_docx
from .validator import rule_feasibility_scan
from .prompts import build_feasibility_messages
from .schemas import ShotFeasibility, FeasibilityReport

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="鲁品开拍", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ai: AIService | None = None

# 简单的内存限流：每个 IP 每分钟最多 20 次 AI 请求
from collections import defaultdict
import time as _time
_rate_buckets: dict = defaultdict(list)
RATE_LIMIT = 20
RATE_WINDOW = 60

def check_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    bucket = _rate_buckets[ip]
    # 清理过期
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    bucket.append(now)


@app.on_event("startup")
def _startup():
    global ai
    try:
        ai = AIService()
    except AIConfigError as e:
        print(f"[启动警告] {e}")
        print("[启动警告] 云端部署时请设置环境变量 LUPIN_API_KEY")


def get_ai(request: Request = None) -> AIService:
    """获取 AI 服务。优先使用请求头中的用户自带配置（不持久化、不记录日志）。
    
    请求头：
    - X-Lupin-Key: 用户 API Key
    - X-Lupin-Base: 用户 API Base URL（可选）
    - X-Lupin-Model: 用户模型名称（可选）
    """
    if request is not None:
        user_key = request.headers.get("X-Lupin-Key", "").strip()
        if user_key:
            user_base = request.headers.get("X-Lupin-Base", "").strip() or None
            try:
                return AIService(api_key=user_key, base_url=user_base)
            except AIConfigError as e:
                raise HTTPException(status_code=400, detail=f"自定义 API 配置无效: {e}")
    if ai is None:
        raise HTTPException(
            status_code=503,
            detail="模型服务未配置：未找到可用的 API Key。请在设置中配置您的 API Key，或联系管理员配置服务默认 Key"
        )
    return ai


def get_request_model(request: Request = None) -> Optional[str]:
    """从请求头获取用户指定的模型名称。"""
    if request is not None:
        m = request.headers.get("X-Lupin-Model", "").strip()
        if m:
            return m
    return None


# ---------- 请求模型 ----------

class ShootInput(BaseModel):
    device: str = "手机"
    people: int = 1
    lighting: str = ""
    location: str = ""
    target_duration_seconds: float = 60
    note: str = ""


class ProductInput(BaseModel):
    product_name: str = Field(..., min_length=1)
    description: str = ""
    selling_points: List[str] = Field(default_factory=list)
    price: str = ""
    audience: str = ""
    evidence_materials: str = ""
    shoot: ShootInput = Field(default_factory=ShootInput)


class IdeasRequest(BaseModel):
    product: ProductInput
    facts: List[dict] = Field(default_factory=list)


class StoryboardRequest(BaseModel):
    product: ProductInput
    direction: str = Field(..., min_length=1)
    facts: List[dict] = Field(default_factory=list)


class RegenShotRequest(BaseModel):
    storyboard: dict
    shot_number: int = Field(..., ge=1)
    instruction: str = Field(..., min_length=1)
    locked: List[int] = Field(default_factory=list)


class RiskCheckRequest(BaseModel):
    storyboard: dict
    evidence_materials: str = ""
    selling_points: List[str] = Field(default_factory=list)
    product: Optional[ProductInput] = None
    facts: List[dict] = Field(default_factory=list)


class ExportRequest(BaseModel):
    storyboard: dict


# ---------- 基础路由 ----------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model": "gpt-6-luna",
        "ai_ready": ai is not None,
        "calls": ai.call_count if ai else 0,
        "total_tokens": ai.total_tokens if ai else 0,
    }


# ---------- 阶段2路由 ----------

def _validate_ideas(data: dict):
    errors = []
    ideas_raw = data.get("ideas")
    if not isinstance(ideas_raw, list):
        return ["[结构] 缺少 ideas 数组"], None
    if len(ideas_raw) != 3:
        return [f"[规则] 创意方向必须为 3 个，实际 {len(ideas_raw)} 个"], None
    ideas = []
    for i, raw in enumerate(ideas_raw):
        try:
            ideas.append(Idea.model_validate(raw))
        except Exception as e:
            errors.append(f"[结构] ideas[{i}]: {e}")
    if errors:
        return errors, None
    return [], ideas


@app.post("/api/ideas")
def create_ideas(req: IdeasRequest, request: Request):
    check_rate_limit(request)
    svc = get_ai(request)
    _model = get_request_model(request)
    product = req.product.model_dump()

    def make_messages(feedback: Optional[str]):
        msgs = build_idea_messages(product, req.facts)
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    try:
        ideas, meta = svc.generate_with_retry(
            make_messages, _validate_ideas, model=_model or DEFAULT_MODEL, temperature=0.7)
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    return {
        "ideas": [i.model_dump() for i in ideas],
        "attempts": meta["attempts"],
        "usage": meta["usage"],
    }


@app.post("/api/storyboard")
def create_storyboard(req: StoryboardRequest, request: Request):
    check_rate_limit(request)
    svc = get_ai(request)
    _model = get_request_model(request)
    product = req.product.model_dump()

    def make_messages(feedback: Optional[str]):
        return build_storyboard_messages(product, req.direction, req.facts, feedback)

    try:
        sb, meta = svc.generate_with_retry(
            make_messages, validate_dict, model=_model or DEFAULT_MODEL, temperature=0.3)
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    return {
        "storyboard": sb.model_dump(),
        "attempts": meta["attempts"],
        "usage": meta["usage"],
    }




# ---------- 阶段3路由 ----------

def _validate_shot(data: dict):
    try:
        shot = Shot.model_validate(data)
        return [], shot
    except Exception as e:
        errs = []
        for err in e.errors():
            loc = "/".join(str(p) for p in err.get("loc", ())) or "(root)"
            errs.append(f"[结构] {loc}: {err.get('msg', '')}")
        return errs, None


@app.post("/api/regenerate-shot")
def regenerate_shot(req: RegenShotRequest):
    check_rate_limit(request)
    svc = get_ai(request)
    _model = get_request_model(request)
    sb = req.storyboard
    shots = sb.get("shots", [])
    if not any(s.get("shot_number") == req.shot_number for s in shots):
        raise HTTPException(status_code=400, detail=f"镜头 {req.shot_number} 不存在")
    if req.shot_number in req.locked:
        raise HTTPException(status_code=400, detail=f"镜头 {req.shot_number} 已锁定，不能重生成")

    def make_messages(feedback: Optional[str]):
        return build_regen_shot_messages(sb, req.shot_number, req.instruction, req.locked, feedback)

    try:
        new_shot, meta = svc.generate_with_retry(
            make_messages, _validate_shot, model=_model or DEFAULT_MODEL, temperature=0.4)
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")

    if new_shot.shot_number != req.shot_number:
        raise HTTPException(status_code=502, detail="模型返回了错误的镜头序号，已丢弃该结果")

    new_sb = json.loads(json.dumps(sb))  # 深拷贝
    for i, s in enumerate(new_sb["shots"]):
        if s.get("shot_number") == req.shot_number:
            new_sb["shots"][i] = new_shot.model_dump()
            break
    # 锁定镜头保护：确认 locked 镜头内容未被改动
    for s_old, s_new in zip(sb["shots"], new_sb["shots"]):
        if s_old.get("shot_number") in req.locked and s_old != s_new:
            raise HTTPException(status_code=502, detail=f"锁定镜头 {s_old.get('shot_number')} 被意外改动，已中止")

    errors, _ = validate_dict(new_sb)
    warning = "; ".join(errors) if errors else ""
    return {"shot": new_shot.model_dump(), "storyboard": new_sb,
            "warning": warning, "attempts": meta["attempts"]}


@app.post("/api/risk-check")
def check_risk(req: RiskCheckRequest, request: Request):
    check_rate_limit(request)
    svc = get_ai(request)
    _model = get_request_model(request)
    try:
        from .prompts import product_block
        pinfo = product_block(req.product.model_dump()) if req.product else ""
        result = risk_check_storyboard(
            svc, req.storyboard,
            evidence=req.evidence_materials,
            input_claims=req.selling_points,
            product_info=pinfo)
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"风险复核失败: {e}")
    # V5+: 事实 grounding 规则层（确定性，不消耗 API）
    try:
        grounding_flags = check_fact_grounding(req.storyboard, req.facts)
        if grounding_flags:
            result["flags"] = result.get("flags", []) + [
                {"text": g["text"], "level": g["level"], "reason": g["reason"],
                 "shot_number": g["shot_number"], "source": "事实依据检查"}
                for g in grounding_flags
            ]
    except Exception:
        pass
    return result


@app.post("/api/export")
def export_plan(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400,
                            detail="分镜未通过校验，无法导出：" + "; ".join(errors[:3]))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_docx(sb.model_dump(), tmp.name)
    return FileResponse(tmp.name, filename="拍摄执行单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class ChecklistRequest(BaseModel):
    storyboard: dict
    shoot_state: dict = Field(default_factory=dict)


@app.post("/api/shooting-checklist")
def export_checklist(req: ChecklistRequest):
    """现场拍摄清单导出（V3）：轻量清单，不替换完整分镜导出。"""
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400,
                            detail="分镜未通过校验，无法导出：" + "; ".join(errors[:3]))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_checklist_docx(sb.model_dump(), req.shoot_state, tmp.name)
    return FileResponse(tmp.name, filename="现场拍摄清单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@app.post("/api/validate")
def validate_sb(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    total = sum(s.duration_seconds for s in sb.shots) if sb else 0
    return {"valid": not errors, "errors": errors, "total_seconds": total,
            "shot_count": len(sb.shots) if sb else 0}


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


@app.post("/api/feasibility-check")
def check_feasibility(req: FeasibilityCheckRequest, request: Request):
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
    svc = get_ai(request)
    _model = get_request_model(request)

    def make_messages(feedback):
        return build_feasibility_messages(sb_dict, feedback)

    try:
        report, meta = svc.generate_with_retry(
            make_messages,
            lambda d: _validate_feasibility(d, shot_numbers),
            model=_model or DEFAULT_MODEL, temperature=0.3)
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
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


# ---------- 静态前端 ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
