"""鲁品开拍 - FastAPI 后端入口。

安全：API Key 只在启动时从配置现读一次、放内存；绝不返回给前端、不写日志。
"""
import json
import os
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
import tempfile
from pydantic import BaseModel, Field

from .ai_service import AIService, AIConfigError, AIFatalError, AIRetryableError, DEFAULT_MODEL
from .schemas import Idea, Storyboard
from .validator import validate_dict
from .prompts import build_idea_messages, build_storyboard_messages, build_regen_shot_messages
from .schemas import Shot
from .risk import risk_check_storyboard, check_fact_grounding
from .exporter import export_docx, export_checklist_docx
from .auth.router import router as auth_router
from .validator import rule_feasibility_scan
from .prompts import build_feasibility_messages
from .schemas import ShotFeasibility, FeasibilityReport

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="鲁品开拍", version="0.1.0")
app.include_router(auth_router)
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
    # Phase 2：组织/权限表与认证组共用 backend/data/lupin.db。
    # 先导入模型注册到同一 Base.metadata，再触发建表（幂等）。
    try:
        from . import models  # noqa: F401
        from .auth.database import get_engine
        get_engine()
    except Exception as e:  # noqa: BLE001
        print(f"[启动警告] 权限体系数据库初始化失败: {e}")


# Phase 2：API v1（组织 / RBAC / 租户隔离新体系），不影响现有 /api/* 接口
# 并行施工中：依赖模块（如 backend.db）尚未落地时跳过，不阻塞主应用启动
try:
    from .api.v1.orgs import router as orgs_router  # noqa: E402
    from .api.v1.works import router as works_router  # noqa: E402
    app.include_router(orgs_router, prefix="/api/v1")
    app.include_router(works_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    print(f"[启动警告] API v1 模块加载跳过（并行施工中）: {type(e).__name__}: {e}")


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


def _cleanup_temp_file(path: str):
    """017: 导出文件下载完成后清理临时文件，避免磁盘持续增长。"""
    try:
        os.remove(path)
    except OSError:
        pass


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
        "model": getattr(ai, "model", DEFAULT_MODEL),
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
def regenerate_shot(req: RegenShotRequest, request: Request):
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
            product_info=pinfo,
            model=_model)
    except (AIFatalError, AIRetryableError, AIConfigError) as e:
        raise HTTPException(status_code=502, detail=str(e))
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
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))


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
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))

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

class DeliveryCheckRequest(BaseModel):
    storyboard: Optional[dict] = None
    product: Optional[dict] = None
    facts: List[dict] = Field(default_factory=list)
    feasibility: Optional[dict] = None
    shoot_state: dict = Field(default_factory=dict)
    # 002: 两套交付标准 —— "plan" 策划方案可交付 / "final" 最终成片可交付
    check_type: Literal["plan", "final"] = "plan"

def _delivery_item(item_id: str, label: str, status: str, reason: str,
                   action: str = "", blocking: bool = True) -> dict:
    """001: 每个检查项显式标注是否为阻断项（blocking）。
    只有阻断项全部通过（done/na），才算可交付；建议项（suggest）不影响 ready。"""
    return {"id": item_id, "label": label, "status": status,
            "reason": reason, "action": action, "blocking": blocking}

def delivery_check(req: DeliveryCheckRequest, check_type: str = "plan") -> dict:
    """根据当前真实数据计算交付状态；不使用硬编码完成率。

    check_type="plan": 策划方案可交付 —— 产品/分镜/时长校验/风险审核为阻断项，
        事实库与拍摄进度仅为建议项。
    check_type="final": 最终成片可交付 —— 在 plan 基础上，拍摄进度与可行性
        也成为阻断项。
    """
    is_final = (check_type or "plan") == "final"
    sb = req.storyboard or {}
    shots = sb.get("shots") or []
    items = []

    # 1. 产品资料（阻断）
    has_product = bool(req.product and req.product.get("product_name"))
    items.append(_delivery_item(
        "product", "产品资料", "done" if has_product else "action",
        "产品名称已填写" if has_product else "尚未填写产品资料",
        "返回产品资料", blocking=True))

    # 2. 可信事实（建议项，不阻断）
    confirmed_facts = [f for f in req.facts
                       if f.get("status") == "confirmed" and f.get("allow_use") is True]
    items.append(_delivery_item(
        "facts", "可信事实",
        "done" if confirmed_facts else ("suggest" if req.facts else "suggest"),
        f"存在 {len(confirmed_facts)} 条已确认且允许使用的事实" if confirmed_facts
        else "建议至少确认一条产品事实（需已确认且允许使用）",
        "检查事实库", blocking=False))

    # 3. 创意与分镜（阻断）
    items.append(_delivery_item(
        "storyboard", "创意与分镜", "done" if shots else "action",
        f"已生成 {len(shots)} 个镜头" if shots else "尚未生成分镜",
        "返回分镜编辑", blocking=True))

    # 4. 时长与校验（阻断，服务端 validate_dict 为准）
    sb_errors, _ = validate_dict(sb) if sb else (["缺少分镜"], None)
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
    shoot_shots = req.shoot_state.get("shots") or {}
    all_shoot_done = (bool(shots) and len(shoot_shots) >= len(shots)
                      and all(v.get("status") == "done" for v in shoot_shots.values()))
    items.append(_delivery_item(
        "shooting", "现场拍摄",
        "done" if all_shoot_done else ("suggest" if shots else "na"),
        "所有镜头已标记完成" if all_shoot_done else "拍摄状态未全部完成",
        "进入现场拍摄", blocking=is_final))

    # 7. 可行性检查（final: 阻断项；plan: 建议项）
    feas_results = (req.feasibility or {}).get("results") or []
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

@app.post("/api/delivery-check")
def delivery_check_route(req: DeliveryCheckRequest):
    return delivery_check(req, req.check_type)
@app.post("/api/export-srt")
def export_srt(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400, detail="分镜未通过校验，无法导出 SRT：" + "; ".join(errors[:3]))
    from .exporter import storyboard_to_srt
    try:
        content = storyboard_to_srt(sb.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tmp = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)
    tmp.write(content.encode("utf-8-sig"))
    tmp.close()
    return FileResponse(tmp.name, filename="字幕.srt", media_type="application/x-subrip",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))

