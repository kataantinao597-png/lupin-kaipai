"""V3.0 D：独立文案优化器 API。

5 种工作（kind）输入输出契约不同，共用版本/风险/权限机制：
  optimize  优化用户已有文案    generate  从零生成新文案
  script    生成完整视频脚本    platform  生成平台发布文案
  adapt     按平台调整同一份文案

全链路（任务书 §6.4）：用户输入 → 模型请求 → 响应解析 → 字段校验 →
内容事实检查 → 风险检查 → 保存草稿 → 用户确认。
失败分别记录：输入校验 400 / 预算 402 / 限流 429 / 无 AI 503 诚实失败 /
模型返回格式错误（不保存草稿）。

铁律：
- 用户未确认绝不覆盖原文或已确认版本 → 所有产出都是 draft 新版本行；
  confirm 只改 status；restore = 新建 confirmed 行。
- 事实纪律：prompt 级禁令（见 copyopt_v1.FACT_RULES）+ 风险检查复用
  risk.rule_scan / risk.check_fact_grounding；风险通过≠广告合规，
  响应里固定带人工复核提示。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...ai.prompts import (
    build_copyopt_messages,
    build_rewrite_paragraph_messages,
    get_prompt_version,
)
from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...ai.secure_messages import _secure_messages
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.deps import get_ai_gateway, get_request_model
from ...core.exceptions import BusinessError
from ...core.idempotency import idempotency_begin
from ...core.response import success
from ...rbac import get_membership, has_permission, assert_can
from ...risk import rule_scan, check_fact_grounding
from ..v1.products import get_product_grounding
from ...repositories.copy_repository import (
    CopyDocNotFoundError,
    CopyRepository,
    CopyVersionNotFoundError,
)

router = APIRouter(prefix="/copy", tags=["v1-copy"])

# 人工复核提示：每次风险检查结果固定携带
REVIEW_REMINDER = (
    "风险检查通过不代表广告合规得到保证。发布前请人工确认文案中的"
    "产品宣称、价格与承诺事项，本工具不保证广告合规。"
)

KINDS = models.COPY_KINDS
MODES = models.COPY_MODES

# kind → 必填校验
_KIND_NEEDS_SOURCE = {"optimize", "adapt"}  # 需要原文


# ---------- 请求/响应模型 ----------

class CopyDocCreate(BaseModel):
    title: str = Field(default="", max_length=256)
    kind: str = Field(default="optimize", max_length=16)
    product_id: Optional[str] = Field(default=None, max_length=32)
    original_text: str = Field(default="", max_length=20000,
                               description="可选原文；提供则自动建档 v1 确认版本")


class OptimizeRequest(BaseModel):
    mode: str = Field(default="natural", max_length=16)
    source_text: str = Field(default="", max_length=20000)
    goal: str = Field(default="", max_length=256)
    style: str = Field(default="", max_length=128)
    audience: str = Field(default="", max_length=128)
    length_limit: str = Field(default="", max_length=64)
    extra_requirements: str = Field(default="", max_length=2000)
    custom_instruction: str = Field(default="", max_length=2000)
    product_id: Optional[str] = Field(default=None, max_length=32)
    fact_ids: list[str] = Field(default_factory=list, max_length=50)


class RewriteParagraphRequest(BaseModel):
    version_id: str = Field(..., max_length=32)
    paragraph_index: int = Field(..., ge=0)
    instruction: str = Field(default="", max_length=2000)
    product_id: Optional[str] = Field(default=None, max_length=32)


# ---------- 内部 helper ----------

def _require_copy_perm(db: Session, user: User, org_id: str,
                       action: str, what: str):
    """文案权限；无权一律 404（防枚举，与 products.py 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _doc_or_404(repo: CopyRepository, org_id: str, doc_id: str):
    try:
        return repo.get_doc(org_id, doc_id)
    except CopyDocNotFoundError:
        raise BusinessError("文案文档不存在",
                            code=error_codes.COPY_DOC_NOT_FOUND,
                            http_status=404)


def _version_out(v: models.CopyVersion) -> dict:
    return {
        "id": v.id, "doc_id": v.doc_id, "version_no": v.version_no,
        "mode": v.mode, "status": v.status,
        "source_text": v.source_text or "", "text": v.text or "",
        "structured": v.structured or {},
        "goal": v.goal or "", "style": v.style or "",
        "audience": v.audience or "", "length_limit": v.length_limit or "",
        "extra_requirements": v.extra_requirements or "",
        "custom_instruction": v.custom_instruction or "",
        "changes_summary": v.changes_summary or "",
        "fact_ids": v.fact_ids or [],
        "risk_status": v.risk_status, "risk_flags": v.risk_flags or [],
        "risk_checked_at": v.risk_checked_at.isoformat()
        if v.risk_checked_at else None,
        "ai_meta": v.ai_meta or {},
        "restored_from": v.restored_from,
        "created_by": v.created_by or "",
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _doc_out(doc: models.CopyDoc,
             versions: list[models.CopyVersion],
             current: Optional[models.CopyVersion]) -> dict:
    return {
        "id": doc.id, "org_id": doc.org_id, "title": doc.title or "",
        "kind": doc.kind, "product_id": doc.product_id,
        "versions": [_version_out(v) for v in versions],
        "current_version": _version_out(current) if current else None,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def _validate_copyopt(kind: str, data: dict):
    """校验模型返回结构。返回 (errors, normalized)。"""
    errors = []
    if not isinstance(data, dict):
        return ["[结构] 返回不是 JSON 对象"], None
    norm: dict[str, Any] = {}
    if kind == "platform":
        for f in ("title", "body", "cta"):
            val = data.get(f)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"[结构] 缺少 {f} 字段")
            else:
                norm[f] = val.strip()
        tags = data.get("hashtags", [])
        if not isinstance(tags, list):
            errors.append("[结构] hashtags 不是数组")
            tags = []
        norm["hashtags"] = [str(t) for t in tags]
        norm["text"] = norm.get("body", "")
    else:
        txt = data.get("text")
        if not isinstance(txt, str) or not txt.strip():
            errors.append("[结构] 缺少 text 字段")
        else:
            norm["text"] = txt.strip()
    changes = data.get("changes", [])
    if not isinstance(changes, list):
        errors.append("[结构] changes 不是数组")
        changes = []
    norm["changes"] = [str(c) for c in changes]
    fn = data.get("fact_notes", "")
    norm["fact_notes"] = str(fn) if isinstance(fn, (str, int, float)) else ""
    if errors:
        return errors, None
    return [], norm


def _validate_fact_ids(db: Session, org_id: str,
                       fact_ids: list[str]) -> list[str]:
    """校验用户勾选的事实 ID 归属本组织；返回有效 ID 列表。"""
    if not fact_ids:
        return []
    rows = db.query(models.Fact.id).filter(
        models.Fact.org_id == org_id,
        models.Fact.id.in_(fact_ids)).all()
    valid = {r[0] for r in rows}
    invalid = [f for f in fact_ids if f not in valid]
    if invalid:
        raise BusinessError(f"事实不存在或不属于本组织: {','.join(invalid[:5])}",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    return [f for f in fact_ids if f in valid]


def _run_risk_check(text: str, structured: dict, kind: str,
                    version_no: int, fact_ids: list[str],
                    usable_facts: list[dict]) -> tuple[str, list]:
    """风险检查：rule_scan + fact grounding（复用 risk.py）。

    返回 (risk_status, flags)。检查本身抛错 → failed，不阻断草稿保存。
    """
    try:
        scan_text = text
        if kind == "platform" and structured:
            scan_text = " ".join([
                str(structured.get("title", "")),
                str(structured.get("body", "")),
                str(structured.get("cta", "")),
                " ".join(str(t) for t in structured.get("hashtags", [])),
            ])
        where = f"文案{kind}v{version_no}"
        flags = rule_scan(scan_text or "", where)
        pseudo = {"shots": [{
            "shot_number": 1,
            "voiceover": scan_text or "",
            "subtitle": "",
            "fact_refs": fact_ids or [],
        }]}
        flags.extend(check_fact_grounding(pseudo, usable_facts or []))
        return models.COPY_RISK_CHECKED, flags
    except Exception:
        return models.COPY_RISK_FAILED, []


def _split_paragraphs(text: str) -> list[str]:
    import re
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "")]
    return [p for p in parts if p]


# ---------- 文档 CRUD ----------

@router.post("/docs", status_code=status.HTTP_201_CREATED)
def create_doc(req: CopyDocCreate,
               org_id: str = Query(...),
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:create", "创建文案文档")
    if req.kind not in KINDS:
        raise BusinessError(f"不支持的文案类型: {req.kind}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    product_id = None
    if req.product_id:
        prod, _ = get_product_grounding(db, org_id, req.product_id)
        product_id = prod["id"]
    repo = CopyRepository(db)
    try:
        doc = repo.create_doc(org_id, req.title, req.kind, product_id,
                              created_by=user.id)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    versions = []
    if req.original_text and req.original_text.strip():
        v1 = repo.add_version(
            org_id, doc.id, mode="natural",
            status=models.COPY_STATUS_CONFIRMED,
            source_text=req.original_text.strip(),
            text=req.original_text.strip(),
            changes_summary="建档原文（用户提供）",
            created_by=user.id)
        versions = [v1]
    return success(_doc_out(doc, versions, versions[0] if versions else None))


@router.get("/docs")
def list_docs(org_id: str = Query(...),
              kind: Optional[str] = Query(default=None),
              user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:read", "查看文案文档")
    if kind is not None and kind not in KINDS:
        raise BusinessError(f"不支持的文案类型: {kind}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    repo = CopyRepository(db)
    docs = repo.list_docs(org_id, kind)
    out = []
    for d in docs:
        cur = repo.current_version(org_id, d.id)
        n = len(repo.list_versions(org_id, d.id))
        out.append({"id": d.id, "title": d.title or "", "kind": d.kind,
                    "product_id": d.product_id,
                    "version_count": n,
                    "current_version_no":
                        cur.version_no if cur else None,
                    "updated_at": d.updated_at.isoformat()
                    if d.updated_at else None})
    return success({"docs": out})


@router.get("/docs/{doc_id}")
def get_doc(doc_id: str,
            org_id: str = Query(...),
            user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:read", "查看文案文档")
    repo = CopyRepository(db)
    doc = _doc_or_404(repo, org_id, doc_id)
    versions = repo.list_versions(org_id, doc_id)
    current = repo.current_version(org_id, doc_id)
    return success(_doc_out(doc, versions, current))


@router.delete("/docs/{doc_id}")
def delete_doc(doc_id: str,
               org_id: str = Query(...),
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:delete", "删除文案文档")
    repo = CopyRepository(db)
    _doc_or_404(repo, org_id, doc_id)
    repo.delete_doc(org_id, doc_id)  # 版本行 CASCADE
    return success({"deleted": doc_id})


@router.delete("/docs/{doc_id}/versions/{version_id}")
def delete_version(doc_id: str, version_id: str,
                   org_id: str = Query(...),
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """放弃草稿：只允许删除 status=draft 的版本；已确认版本不可删（防误操作）。"""
    _require_copy_perm(db, user, org_id, "copy:update", "放弃文案草稿")
    repo = CopyRepository(db)
    _doc_or_404(repo, org_id, doc_id)
    try:
        v = repo.get_version(org_id, doc_id, version_id)
    except CopyVersionNotFoundError:
        raise BusinessError("文案版本不存在",
                            code=error_codes.COPY_VERSION_NOT_FOUND,
                            http_status=404)
    if v.status != models.COPY_STATUS_DRAFT:
        raise BusinessError("只有草稿版本可以放弃，已确认版本请用“恢复”覆盖",
                            code=error_codes.INVALID_PARAM, http_status=400)
    db.delete(v)
    db.commit()
    return success({"deleted": version_id})


# ---------- AI 生成：优化/生成/脚本/发布文案/平台适配 ----------

@router.post("/docs/{doc_id}/optimize")
def optimize(doc_id: str,
             req: OptimizeRequest,
             request: Request,
             org_id: str = Query(...),
             user: User = Depends(get_current_user),
             db: Session = Depends(get_db),
             gw=Depends(get_ai_gateway)):
    _require_copy_perm(db, user, org_id, "copy:update", "生成文案版本")
    repo = CopyRepository(db)
    doc = _doc_or_404(repo, org_id, doc_id)
    kind = doc.kind

    # 1) 输入校验
    if req.mode not in MODES:
        raise BusinessError(f"不支持的优化模式: {req.mode}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    source = (req.source_text or "").strip()
    if kind in _KIND_NEEDS_SOURCE and not source:
        # 允许从当前确认版本取原文
        cur = repo.current_version(org_id, doc_id)
        if cur and cur.text:
            source = cur.text
        else:
            raise BusinessError("该类型需要提供原文（或先确认一个版本）",
                                code=error_codes.INVALID_PARAM,
                                http_status=400)
    if req.mode == "custom" and not (req.custom_instruction or "").strip():
        raise BusinessError("自定义模式需要填写自定义优化要求",
                            code=error_codes.INVALID_PARAM, http_status=400)

    # 2) 产品 grounding（可选）：只取已确认且允许使用的事实
    product_name, no_expand, usable_facts = "", "", []
    product_id = req.product_id or doc.product_id
    if product_id:
        prod, usable_facts = get_product_grounding(db, org_id, product_id)
        product_name = prod.get("name", "")
        # 取产品的宣传限制
        p_row = db.query(models.Product).filter(
            models.Product.id == prod["id"]).first()
        if p_row:
            no_expand = p_row.no_expand_claims or ""
        product_id = prod["id"]
    fact_ids = _validate_fact_ids(db, org_id, req.fact_ids or [])

    # 3) 幂等（V2.1 #3 同模式）：同 key 同体直接回放
    _model = get_request_model(request)
    idem = idempotency_begin("copy_optimize", request,
                             {"doc_id": doc_id, **req.model_dump()},
                             scope=_model or "", user_id=user.id)
    if idem.replay is not None:
        return idem.replay

    def make_messages(feedback):
        # S-3 同类闭合：与 runner 同款隔离
        return _secure_messages(build_copyopt_messages(
            kind, req.mode, source_text=source,
            goal=req.goal.strip(), style=req.style.strip(),
            audience=req.audience.strip(),
            length_limit=req.length_limit.strip(),
            extra_requirements=req.extra_requirements.strip(),
            custom_instruction=req.custom_instruction.strip(),
            facts=usable_facts, product_name=product_name,
            no_expand_claims=no_expand))

    # 4) 模型请求 → 响应解析 → 字段校验
    try:
        try:
            result, meta = gw.generate_with_retry(
                "copy_optimize", make_messages,
                lambda d: _validate_copyopt(kind, d),
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("copy_optimize"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            # 无 AI 可用时诚实失败，不伪造内容，不留草稿
            raise HTTPException(
                status_code=503,
                detail=f"AI 文案服务暂不可用（{e}），请稍后再试")
        norm = result  # 已通过 _validate_copyopt
        text = norm.get("text", "")
        structured = {}
        if kind == "platform":
            structured = {"title": norm.get("title", ""),
                          "body": norm.get("body", ""),
                          "hashtags": norm.get("hashtags", []),
                          "cta": norm.get("cta", "")}

        # 5) 风险检查（复用 risk.py；失败只标记不阻断保存）
        next_no = repo.next_version_no(doc_id)
        risk_status, flags = _run_risk_check(
            text, structured, kind, next_no, fact_ids, usable_facts)

        # 6) 保存草稿（status=draft；用户未确认前原文/已确认版本不受影响）
        changes = norm.get("changes", [])
        v = repo.add_version(
            org_id, doc_id, mode=req.mode,
            status=models.COPY_STATUS_DRAFT,
            source_text=source, text=text, structured=structured,
            goal=req.goal.strip(), style=req.style.strip(),
            audience=req.audience.strip(),
            length_limit=req.length_limit.strip(),
            extra_requirements=req.extra_requirements.strip(),
            custom_instruction=req.custom_instruction.strip(),
            changes_summary="\n".join(f"- {c}" for c in changes),
            fact_ids=fact_ids,
            risk_status=risk_status, risk_flags=flags,
            risk_checked_at=datetime.now(timezone.utc)
            if risk_status == models.COPY_RISK_CHECKED else None,
            ai_meta={"model": _model or "",
                     "attempts": meta.get("attempts"),
                     "prompt_version": get_prompt_version("copy_optimize"),
                     "fact_notes": norm.get("fact_notes", "")},
            created_by=user.id)
        resp = success({
            "version": _version_out(v),
            "review_reminder": REVIEW_REMINDER,
        })
    except Exception:
        idem.abort()  # 失败不留脏记录
        raise
    return idem.finish(resp)


# ---------- 版本操作：确认 / 恢复 / 段落重写 ----------

@router.post("/docs/{doc_id}/versions/{version_id}/confirm")
def confirm_version(doc_id: str, version_id: str,
                    org_id: str = Query(...),
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:update", "确认文案版本")
    repo = CopyRepository(db)
    _doc_or_404(repo, org_id, doc_id)
    try:
        v = repo.confirm_version(org_id, doc_id, version_id)
    except CopyVersionNotFoundError:
        raise BusinessError("文案版本不存在",
                            code=error_codes.COPY_VERSION_NOT_FOUND,
                            http_status=404)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    current = repo.current_version(org_id, doc_id)
    return success({"version": _version_out(v),
                    "current_version": _version_out(current)
                    if current else None,
                    "review_reminder": REVIEW_REMINDER})


@router.post("/docs/{doc_id}/versions/{version_id}/restore")
def restore_version(doc_id: str, version_id: str,
                    org_id: str = Query(...),
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """恢复版本 = 新建 confirmed 行（复制目标版本内容），旧行不动。"""
    _require_copy_perm(db, user, org_id, "copy:update", "恢复文案版本")
    repo = CopyRepository(db)
    _doc_or_404(repo, org_id, doc_id)
    try:
        src = repo.get_version(org_id, doc_id, version_id)
    except CopyVersionNotFoundError:
        raise BusinessError("文案版本不存在",
                            code=error_codes.COPY_VERSION_NOT_FOUND,
                            http_status=404)
    v = repo.add_version(
        org_id, doc_id, mode=src.mode,
        status=models.COPY_STATUS_CONFIRMED,
        source_text=src.text or "", text=src.text or "",
        structured=dict(src.structured or {}),
        goal=src.goal or "", style=src.style or "",
        audience=src.audience or "",
        length_limit=src.length_limit or "",
        extra_requirements=src.extra_requirements or "",
        custom_instruction=src.custom_instruction or "",
        changes_summary=f"恢复自 v{src.version_no}（用户操作）",
        fact_ids=list(src.fact_ids or []),
        risk_status=src.risk_status,
        risk_flags=list(src.risk_flags or []),
        risk_checked_at=src.risk_checked_at,
        ai_meta=dict(src.ai_meta or {}),
        restored_from=src.id,
        created_by=user.id)
    return success({"version": _version_out(v),
                    "review_reminder": REVIEW_REMINDER})


@router.post("/docs/{doc_id}/rewrite-paragraph")
def rewrite_paragraph(doc_id: str,
                      req: RewriteParagraphRequest,
                      request: Request,
                      org_id: str = Query(...),
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db),
                      gw=Depends(get_ai_gateway)):
    """选中段落单独重写：只改该段，其他段落原样保留 → draft 新版本。"""
    _require_copy_perm(db, user, org_id, "copy:update", "段落重写")
    repo = CopyRepository(db)
    doc = _doc_or_404(repo, org_id, doc_id)
    try:
        src = repo.get_version(org_id, doc_id, req.version_id)
    except CopyVersionNotFoundError:
        raise BusinessError("文案版本不存在",
                            code=error_codes.COPY_VERSION_NOT_FOUND,
                            http_status=404)
    paras = _split_paragraphs(src.text or "")
    if req.paragraph_index >= len(paras):
        raise BusinessError(
            f"段落序号越界（共 {len(paras)} 段）",
            code=error_codes.INVALID_PARAM, http_status=400)

    product_name, no_expand_unused, usable_facts = "", "", []
    product_id = req.product_id or None
    if product_id:
        prod, usable_facts = get_product_grounding(db, org_id, product_id)
        product_name = prod.get("name", "")
        product_id = prod["id"]

    _model = get_request_model(request)
    idem = idempotency_begin("copy_rewrite_paragraph", request,
                             {"doc_id": doc_id, **req.model_dump()},
                             scope=_model or "", user_id=user.id)
    if idem.replay is not None:
        return idem.replay

    def make_messages(feedback):
        # S-3 同类闭合：与 runner 同款隔离
        return _secure_messages(build_rewrite_paragraph_messages(
            src.text or "", paras[req.paragraph_index],
            req.instruction.strip(), usable_facts))

    try:
        try:
            result, meta = gw.generate_with_retry(
                "copy_optimize", make_messages,
                lambda d: _validate_copyopt(doc.kind, d),
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("copy_optimize"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(
                status_code=503,
                detail=f"AI 文案服务暂不可用（{e}），请稍后再试")
        text = result.get("text", "")
        next_no = repo.next_version_no(doc_id)
        risk_status, flags = _run_risk_check(
            text, {}, doc.kind, next_no, list(src.fact_ids or []),
            usable_facts)
        changes = result.get("changes", [])
        v = repo.add_version(
            org_id, doc_id, mode=src.mode,
            status=models.COPY_STATUS_DRAFT,
            source_text=src.text or "", text=text,
            structured=dict(src.structured or {}),
            goal=src.goal or "", style=src.style or "",
            audience=src.audience or "",
            length_limit=src.length_limit or "",
            extra_requirements=src.extra_requirements or "",
            changes_summary=(
                f"段落 {req.paragraph_index + 1} 单独重写\n" +
                "\n".join(f"- {c}" for c in changes)),
            fact_ids=list(src.fact_ids or []),
            risk_status=risk_status, risk_flags=flags,
            risk_checked_at=datetime.now(timezone.utc)
            if risk_status == models.COPY_RISK_CHECKED else None,
            ai_meta={"model": _model or "",
                     "attempts": meta.get("attempts"),
                     "prompt_version": get_prompt_version("copy_optimize"),
                     "paragraph_index": req.paragraph_index,
                     "fact_notes": result.get("fact_notes", "")},
            created_by=user.id)
        resp = success({
            "version": _version_out(v),
            "review_reminder": REVIEW_REMINDER,
        })
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)
