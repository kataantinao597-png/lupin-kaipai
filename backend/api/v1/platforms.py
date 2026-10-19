"""V3.0 E：多平台内容适配 API。

从已确认文案版本派生独立的平台发布版本（抖音/快手/小红书/视频号）。

铁律：
- 首期只做可检查、可复制、可导出的发布内容；绝不声称自动发布或账号数据读取。
- 平台规则来自 backend/content/platform_rules.yaml 配置文件，不写死在代码里；
  界面与响应固定带"平台规则可能变化，仅供参考"免责声明。
- 每个平台版本独立保存人工调整；修改一个版本绝不触及其他平台版本。
- 事实纪律：只调表达，不添加原文/已确认事实之外的任何事实主张。
- 无 AI 时诚实 503，不伪造内容，不留脏记录。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...ai.prompts import (
    build_platform_messages,
    get_prompt_version,
    validate_platform_result,
)
from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.deps import get_ai_gateway, get_request_model
from ...core.exceptions import BusinessError
from ...core.idempotency import idempotency_begin
from ...core.response import success
from .copyopt import (
    REVIEW_REMINDER,
    _require_copy_perm,
    _run_risk_check,
    _validate_fact_ids,
)
from ..v1.products import get_product_grounding
from ...repositories.copy_repository import (
    CopyRepository,
    CopyVersionNotFoundError,
)
from ...repositories.platform_repository import (
    PlatformRepository,
    PlatformVersionNotFoundError,
)

router = APIRouter(prefix="/platforms", tags=["v1-platform"])

PLATFORMS = models.PLATFORM_IDS

_RULES_CACHE: dict | None = None


def get_platform_rules() -> dict:
    """读取平台规则配置（含免责声明）。"""
    global _RULES_CACHE
    if _RULES_CACHE is None:
        import yaml
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(os.path.dirname(here)),
                            "content", "platform_rules.yaml")
        with open(path, encoding="utf-8") as f:
            _RULES_CACHE = yaml.safe_load(f) or {}
    return _RULES_CACHE


def _optional_gateway(request):
    """懒加载 AI 网关；未配置/失败返回 None（手动模式不经过网关）。

    既定模式（见 fact_import._optional_gateway）：get_ai_gateway 作为 Depends
    硬依赖时，无 key 直接 503，连手动模式都进不来，故在路由内懒加载。
    同时尊重 app.dependency_overrides（测试注入 fake 网关）。
    """
    from ...core.deps import get_ai_gateway as _get_gw
    try:
        from backend.app import app as _app  # 延迟导入，避免循环
        ov = _app.dependency_overrides.get(_get_gw)
    except Exception:
        ov = None
    try:
        if ov is not None:
            r = ov()
            return next(r) if hasattr(r, "__next__") else r
        gen = _get_gw(request)
        try:
            return next(gen)
        except Exception:
            gen.close()
            raise
    except Exception:
        return None


def _rules_for(platform: str) -> dict:
    rules = get_platform_rules().get("platforms", {})
    if platform not in rules:
        raise BusinessError(
            f"不支持的目标平台: {platform}",
            code=error_codes.PLATFORM_NOT_SUPPORTED, http_status=400)
    return rules[platform]


def _pv_or_404(repo: PlatformRepository, org_id: str, pv_id: str):
    try:
        return repo.get(org_id, pv_id)
    except PlatformVersionNotFoundError:
        raise BusinessError("平台版本不存在",
                            code=error_codes.PLATFORM_VERSION_NOT_FOUND,
                            http_status=404)


def _pv_out(pv: models.PlatformVersion) -> dict:
    return {
        "id": pv.id,
        "doc_id": pv.doc_id,
        "copy_version_id": pv.copy_version_id,
        "source_version_no": pv.source_version_no,
        "platform": pv.platform,
        "revision_no": pv.revision_no,
        "title": pv.title,
        "body": pv.body,
        "hashtags": pv.hashtags or [],
        "cover_text": pv.cover_text,
        "pinned_comment": pv.pinned_comment,
        "cta": pv.cta,
        "review_status": pv.review_status,
        "changes_summary": pv.changes_summary,
        "fact_ids": pv.fact_ids or [],
        "risk_status": pv.risk_status,
        "risk_flags": pv.risk_flags or [],
        "ai_meta": pv.ai_meta or {},
        "created_by": pv.created_by,
        "created_at": pv.created_at.isoformat() if pv.created_at else None,
        "updated_at": pv.updated_at.isoformat() if pv.updated_at else None,
    }


def _export_text(pv: models.PlatformVersion, platform_name: str) -> str:
    lines = [
        "【%s】发布版本（修订 %s）" % (platform_name, pv.revision_no),
        "来源：文案版本 v%s" % pv.source_version_no,
        "",
        "标题：%s" % pv.title,
        "",
        "正文：\n%s" % pv.body,
        "",
        "话题：%s" % " ".join("#" + t for t in (pv.hashtags or [])),
        "封面文案：%s" % pv.cover_text,
        "置顶评论建议：%s" % pv.pinned_comment,
        "行动引导：%s" % pv.cta,
        "",
        "审核状态：%s" % ("已人工审核" if pv.review_status == models.PLATFORM_REVIEW_REVIEWED else "未人工审核"),
        "—— 平台规则可能变化，以上内容仅供参考；发布前请人工确认 ——",
    ]
    return "\n".join(lines)


class PlatformCreateRequest(BaseModel):
    copy_version_id: str = Field(..., max_length=32)
    platform: str = Field(..., max_length=32)
    use_ai: bool = Field(default=True)
    title: str = Field(default="", max_length=256)
    body: str = Field(default="", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    cover_text: str = Field(default="", max_length=256)
    pinned_comment: str = Field(default="", max_length=2000)
    cta: str = Field(default="", max_length=256)
    product_id: Optional[str] = Field(default=None, max_length=32)
    fact_ids: list[str] = Field(default_factory=list, max_length=50)


class PlatformPatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    body: Optional[str] = Field(default=None, max_length=5000)
    hashtags: Optional[list[str]] = Field(default=None, max_length=30)
    cover_text: Optional[str] = Field(default=None, max_length=256)
    pinned_comment: Optional[str] = Field(default=None, max_length=2000)
    cta: Optional[str] = Field(default=None, max_length=256)


class PlatformReviewRequest(BaseModel):
    reviewed: bool = Field(default=True)


@router.get("/rules")
def platform_rules(org_id: str = Query(...),
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:read", "查看平台规则")
    return success(get_platform_rules())


@router.post("/copy/docs/{doc_id}/platforms", status_code=status.HTTP_201_CREATED)
def create_platform_version(doc_id: str, req: PlatformCreateRequest,
                            request: Request,
                            org_id: str = Query(...),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:update", "生成平台版本")
    if req.platform not in PLATFORMS:
        raise BusinessError(
            f"不支持的目标平台: {req.platform}",
            code=error_codes.PLATFORM_NOT_SUPPORTED, http_status=400)
    rules = _rules_for(req.platform)
    copy_repo = CopyRepository(db)
    try:
        doc = copy_repo.get_doc(org_id, doc_id)
    except Exception:
        raise BusinessError("文案文档不存在",
                            code=error_codes.COPY_DOC_NOT_FOUND,
                            http_status=404)
    try:
        src = copy_repo.get_version(org_id, doc_id, req.copy_version_id)
    except CopyVersionNotFoundError:
        raise BusinessError("文案版本不存在",
                            code=error_codes.COPY_VERSION_NOT_FOUND,
                            http_status=404)
    if src.status != models.COPY_STATUS_CONFIRMED:
        raise BusinessError("只能从已确认的文案版本生成平台版本",
                            code=error_codes.INVALID_PARAM, http_status=400)

    plat_repo = PlatformRepository(db)

    if not req.use_ai:
        title, body = req.title.strip(), req.body.strip()
        if not title or not body:
            raise BusinessError("手动模式需要填写标题和正文",
                                code=error_codes.INVALID_PARAM,
                                http_status=400)
        pv = plat_repo.create(
            org_id, doc_id, src.id, src.version_no, req.platform,
            title=title, body=body,
            hashtags=[t.strip().lstrip("#") for t in req.hashtags if t.strip()],
            cover_text=req.cover_text.strip(),
            pinned_comment=req.pinned_comment.strip(),
            cta=req.cta.strip(),
            ai_meta={"mode": "manual"},
            created_by=user.id)
        return success({"platform_version": _pv_out(pv),
                        "review_reminder": REVIEW_REMINDER})

    gw = _optional_gateway(request)
    if req.use_ai and gw is None:
        # 无 key 时 AI 模式诚实失败（与 Depends 硬依赖时的 503 语义一致）
        from ...core.exceptions import AIConfigError_ as _AICfgErr
        raise _AICfgErr("模型服务未配置：未找到可用的 API Key")

    product_name, no_expand, usable_facts = "", "", []
    product_id = req.product_id or doc.product_id
    if product_id:
        prod, usable_facts = get_product_grounding(db, org_id, product_id)
        product_name = prod.get("name", "")
        p_row = db.query(models.Product).filter(
            models.Product.id == prod["id"]).first()
        if p_row:
            no_expand = p_row.no_expand_claims or ""
    fact_ids = _validate_fact_ids(db, org_id, req.fact_ids or [])

    _model = get_request_model(request)
    idem = idempotency_begin("platform_adapt", request,
                             {"doc_id": doc_id, **req.model_dump()},
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay

    def make_messages(feedback):
        src_structured = src.structured if isinstance(src.structured, dict) else {}
        return build_platform_messages(
            source_text=src.text or "",
            source_structured=src_structured,
            platform=req.platform, rules=rules,
            facts=usable_facts, product_name=product_name,
            no_expand_claims=no_expand)

    try:
        try:
            result, meta = gw.generate_with_retry(
                "platform_adapt", make_messages,
                validate_platform_result,
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("platform_adapt"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(
                status_code=503,
                detail="AI 平台适配服务暂不可用（%s），请稍后再试" % e)
        norm = result
        structured = {"title": norm["title"], "body": norm["body"],
                      "hashtags": norm["hashtags"], "cta": norm["cta"]}
        risk_status, flags = _run_risk_check(
            "", structured, "platform", src.version_no, fact_ids,
            usable_facts)
        pv = plat_repo.create(
            org_id, doc_id, src.id, src.version_no, req.platform,
            title=norm["title"], body=norm["body"],
            hashtags=norm["hashtags"],
            cover_text=norm["cover_text"],
            pinned_comment=norm["pinned_comment"],
            cta=norm["cta"],
            changes_summary="\n".join("- " + c for c in norm["changes"]),
            fact_ids=fact_ids,
            risk_status=risk_status, risk_flags=flags,
            ai_meta={"model": _model or "",
                     "attempts": meta.get("attempts"),
                     "prompt_version": get_prompt_version("platform_adapt"),
                     "fact_notes": norm.get("fact_notes", "")},
            created_by=user.id)
        resp = success({"platform_version": _pv_out(pv),
                        "review_reminder": REVIEW_REMINDER})
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)


@router.get("/copy/docs/{doc_id}/platforms")
def list_platform_versions(doc_id: str,
                           org_id: str = Query(...),
                           platform: Optional[str] = Query(default=None),
                           user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:read", "查看平台版本")
    repo = PlatformRepository(db)
    items = repo.list_by_doc(org_id, doc_id, platform=platform)
    rules = get_platform_rules()
    return success({
        "items": [_pv_out(pv) for pv in items],
        "disclaimer": rules.get("disclaimer", ""),
    })


@router.get("/{pv_id}")
def get_platform_version(pv_id: str,
                         org_id: str = Query(...),
                         user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:read", "查看平台版本")
    pv = _pv_or_404(PlatformRepository(db), org_id, pv_id)
    return success({"platform_version": _pv_out(pv)})


@router.patch("/{pv_id}")
def update_platform_version(pv_id: str, req: PlatformPatchRequest,
                            org_id: str = Query(...),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """人工调整：只更新本行，其他平台版本不受影响。"""
    _require_copy_perm(db, user, org_id, "copy:update", "编辑平台版本")
    repo = PlatformRepository(db)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "hashtags" in fields:
        fields["hashtags"] = [t.strip().lstrip("#") for t in fields["hashtags"]
                              if str(t).strip()]
    for k in ("title", "body", "cover_text", "pinned_comment", "cta"):
        if k in fields and isinstance(fields[k], str):
            fields[k] = fields[k].strip()
    pv = repo.update(org_id, pv_id, **fields)
    return success({"platform_version": _pv_out(pv)})


@router.post("/{pv_id}/review")
def review_platform_version(pv_id: str, req: PlatformReviewRequest,
                            org_id: str = Query(...),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:update", "审核平台版本")
    repo = PlatformRepository(db)
    pv = repo.update(
        org_id, pv_id,
        review_status=(models.PLATFORM_REVIEW_REVIEWED if req.reviewed
                       else models.PLATFORM_REVIEW_UNREVIEWED))
    return success({"platform_version": _pv_out(pv)})


@router.get("/{pv_id}/export")
def export_platform_version(pv_id: str,
                            org_id: str = Query(...),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """导出可复制/下载的发布文本。"""
    _require_copy_perm(db, user, org_id, "copy:read", "导出平台版本")
    pv = _pv_or_404(PlatformRepository(db), org_id, pv_id)
    rules = get_platform_rules().get("platforms", {})
    name = rules.get(pv.platform, {}).get("name", pv.platform)
    return success({"text": _export_text(pv, name),
                    "platform": pv.platform,
                    "platform_name": name})


@router.delete("/{pv_id}")
def delete_platform_version(pv_id: str,
                            org_id: str = Query(...),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    _require_copy_perm(db, user, org_id, "copy:delete", "删除平台版本")
    PlatformRepository(db).delete(org_id, pv_id)
    return success({"deleted": True})
