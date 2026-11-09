"""V3.0 H：效果复盘 API。

发布后的效果跟踪 + 人工复盘闭环。

铁律：
- 所有数据人工录入，绝不编造：每条指标必带来源（人工录入/平台截图导入）
  与数据时间范围；无数据时展示空状态"暂无数据"，绝不展示虚假图表。
- 直接观察与 AI 推测严格区分标注。
- AI 改进建议只存建议表（review_suggestions），绝不自动覆盖已确认文案、
  已发布版本与任何已确认内容，只展示为待人工采纳。
- 发布记录状态流转复用发布中心 transition 接口（published → reviewed 已合法，
  跳步后端拒绝），本模块不另建状态机。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...ai.prompts import get_prompt_version
from ...ai.prompts.review_v1 import (
    build_review_suggest_messages,
    validate_review_suggest_result,
)
from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.secure_messages import _secure_messages
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.deps import get_ai_gateway, get_request_model
from ...core.exceptions import AIConfigError_ as _AICfgErr
from ...core.exceptions import BusinessError
from ...core.idempotency import idempotency_begin
from ...core.response import success
from ...rbac import assert_can, get_membership, has_permission

router = APIRouter(prefix="/review", tags=["v1-review"])

# 灵活指标字段白名单（按平台常见口径；未知字段允许但会原样存）
METRIC_FIELDS = ("plays", "likes", "comments", "shares", "favorites",
                 "followers_gain", "watch_time_seconds")


# ---------- 内部 helper ----------

def _require_review_perm(db: Session, user: User, org_id: str,
                         action: str, what: str):
    """复盘权限（复用 publish 权限体系；无权一律 404 防枚举）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _rec_or_404(db: Session, org_id: str, rec_id: str) -> models.PublishRecord:
    rec = (db.query(models.PublishRecord)
           .filter(models.PublishRecord.id == rec_id,
                   models.PublishRecord.org_id == org_id).first())
    if rec is None:
        raise BusinessError("发布记录不存在",
                            code=error_codes.PUBLISH_RECORD_NOT_FOUND,
                            http_status=404)
    return rec


def _require_reviewable(rec: models.PublishRecord):
    """只有已发布/已复盘的记录才能录入指标、写复盘、要 AI 建议。"""
    if rec.status not in (models.PUBLISH_PUBLISHED, models.PUBLISH_REVIEWED):
        raise BusinessError(
            "只有「已发布」的记录才能进行效果复盘（当前状态：%s）" % rec.status,
            code=error_codes.REVIEW_ONLY_PUBLISHED, http_status=400)


def _metric_out(m: models.EffectMetric) -> dict:
    return {
        "id": m.id,
        "source": m.source,
        "source_label": models.METRIC_SOURCE_LABELS.get(m.source, m.source),
        "date_start": m.date_start.isoformat() if m.date_start else None,
        "date_end": m.date_end.isoformat() if m.date_end else None,
        "metrics": m.metrics or {},
        "note": m.note or "",
        "recorded_at": m.recorded_at.isoformat() if m.recorded_at else None,
        "created_by": m.created_by,
    }


def _suggestion_out(s: models.ReviewSuggestion) -> dict:
    return {
        "id": s.id,
        "suggestion_text": s.suggestion_text,
        "status": s.status,
        "ai_meta": s.ai_meta or {},
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _parse_metrics(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise BusinessError("指标不能为空",
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)
    if len(raw) > 32:
        raise BusinessError("指标字段过多（最多 32 个）",
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)
    out: Dict[str, float] = {}
    for k, v in raw.items():
        name = str(k).strip()[:64]
        if not name:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise BusinessError("指标「%s」的值必须是数字" % name,
                                code=error_codes.EFFECT_METRIC_INVALID,
                                http_status=400)
        if num < 0 or num != num:  # 负数/NaN 拒绝
            raise BusinessError("指标「%s」的值非法" % name,
                                code=error_codes.EFFECT_METRIC_INVALID,
                                http_status=400)
        out[name] = num
    if not out:
        raise BusinessError("指标不能为空",
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)
    return out


def _parse_date(value: Optional[str], field: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise BusinessError("%s格式非法（应为 YYYY-MM-DD）" % field,
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)


def _optional_gateway(request):
    """懒加载 AI 网关；未配置返回 None（无 key 诚实 503）。"""
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
            return None
    except Exception:
        return None


# ---------- 请求体 ----------

class MetricCreate(BaseModel):
    source: str = Field(default=models.METRIC_SOURCE_MANUAL, max_length=32)
    date_start: Optional[str] = Field(default=None, max_length=16)
    date_end: Optional[str] = Field(default=None, max_length=16)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = Field(default="", max_length=2000)


class ReviewNoteUpdate(BaseModel):
    review_note: str = Field(default="", max_length=10000)


class SuggestionDecision(BaseModel):
    decision: str = Field(..., max_length=16)  # adopted / dismissed


# ---------- 指标录入与历史 ----------

@router.post("/records/{rec_id}/metrics", status_code=status.HTTP_201_CREATED)
def create_metric(rec_id: str, req: MetricCreate,
                  org_id: str = Query(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _require_review_perm(db, user, org_id, "publish:update", "录入效果指标")
    rec = _rec_or_404(db, org_id, rec_id)
    _require_reviewable(rec)
    if req.source not in models.METRIC_SOURCES:
        raise BusinessError("指标来源非法（仅支持：人工录入/平台截图导入）",
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)
    ds = _parse_date(req.date_start, "开始日期")
    de = _parse_date(req.date_end, "结束日期")
    if ds and de and ds > de:
        raise BusinessError("开始日期不能晚于结束日期",
                            code=error_codes.EFFECT_METRIC_INVALID,
                            http_status=400)
    parsed = _parse_metrics(req.metrics)
    m = models.EffectMetric(
        org_id=org_id, publish_record_id=rec.id, source=req.source,
        date_start=ds, date_end=de, metrics=parsed,
        note=(req.note or "").strip(), created_by=user.id)
    db.add(m)
    db.commit()
    db.refresh(m)
    return success({"metric": _metric_out(m)})


@router.get("/records/{rec_id}/metrics")
def list_metrics(rec_id: str,
                 org_id: str = Query(...),
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    _require_review_perm(db, user, org_id, "publish:read", "查看效果指标")
    rec = _rec_or_404(db, org_id, rec_id)
    rows = (db.query(models.EffectMetric)
            .filter(models.EffectMetric.publish_record_id == rec.id,
                    models.EffectMetric.org_id == org_id)
            .order_by(models.EffectMetric.recorded_at.asc(),
                      models.EffectMetric.created_at.asc()).all())
    return success({"metrics": [_metric_out(m) for m in rows],
                    "has_data": bool(rows)})


@router.get("/records/{rec_id}/metrics/latest")
def latest_metric(rec_id: str,
                  org_id: str = Query(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _require_review_perm(db, user, org_id, "publish:read", "查看效果指标")
    rec = _rec_or_404(db, org_id, rec_id)
    m = (db.query(models.EffectMetric)
         .filter(models.EffectMetric.publish_record_id == rec.id,
                 models.EffectMetric.org_id == org_id)
         .order_by(models.EffectMetric.recorded_at.desc(),
                   models.EffectMetric.created_at.desc()).first())
    if m is None:
        return success({"metric": None, "has_data": False})
    return success({"metric": _metric_out(m), "has_data": True})


# ---------- 复盘笔记 ----------

@router.patch("/records/{rec_id}/review-note")
def save_review_note(rec_id: str, req: ReviewNoteUpdate,
                     org_id: str = Query(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    _require_review_perm(db, user, org_id, "publish:update", "撰写复盘结论")
    rec = _rec_or_404(db, org_id, rec_id)
    _require_reviewable(rec)
    rec.review_note = (req.review_note or "").strip()
    db.commit()
    return success({"review_note": rec.review_note})


@router.get("/records/{rec_id}/review")
def get_review(rec_id: str,
               org_id: str = Query(...),
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """复盘总览：笔记 + 指标历史 + AI 建议（无数据即空状态）。"""
    _require_review_perm(db, user, org_id, "publish:read", "查看复盘")
    rec = _rec_or_404(db, org_id, rec_id)
    metrics = (db.query(models.EffectMetric)
               .filter(models.EffectMetric.publish_record_id == rec.id,
                       models.EffectMetric.org_id == org_id)
               .order_by(models.EffectMetric.recorded_at.asc()).all())
    sugs = (db.query(models.ReviewSuggestion)
            .filter(models.ReviewSuggestion.publish_record_id == rec.id,
                    models.ReviewSuggestion.org_id == org_id)
            .order_by(models.ReviewSuggestion.created_at.asc()).all())
    return success({
        "record_id": rec.id,
        "status": rec.status,
        "review_note": rec.review_note or "",
        "reviewed_at": rec.reviewed_at.isoformat() if rec.reviewed_at else None,
        "metrics": [_metric_out(m) for m in metrics],
        "suggestions": [_suggestion_out(s) for s in sugs],
        "has_data": bool(metrics),
    })


# ---------- AI 改进建议 ----------

@router.post("/records/{rec_id}/suggest")
def suggest_improvements(rec_id: str,
                         request: Request,
                         org_id: str = Query(...),
                         db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """AI 改进建议（可选、耗配额）。

    只生成建议文本存入 review_suggestions；绝不修改任何已确认内容。
    无 AI 配置时诚实 503。
    """
    _require_review_perm(db, user, org_id, "publish:update", "获取 AI 复盘建议")
    rec = _rec_or_404(db, org_id, rec_id)
    _require_reviewable(rec)

    gw = _optional_gateway(request)
    if gw is None:
        raise _AICfgErr("模型服务未配置：未找到可用的 API Key")

    metrics = (db.query(models.EffectMetric)
               .filter(models.EffectMetric.publish_record_id == rec.id,
                       models.EffectMetric.org_id == org_id)
               .order_by(models.EffectMetric.recorded_at.asc()).all())
    history = []
    for m in metrics:
        rng = None
        if m.date_start or m.date_end:
            rng = "%s ~ %s" % (
                m.date_start.isoformat() if m.date_start else "?",
                m.date_end.isoformat() if m.date_end else "?")
        history.append({
            "source_label": models.METRIC_SOURCE_LABELS.get(m.source,
                                                           m.source),
            "date_range": rng,
            "metrics": m.metrics or {},
            "note": m.note or "",
        })

    _model = get_request_model(request)
    idem = idempotency_begin("review_suggest", request,
                             {"rec_id": rec_id}, scope=_model or "",
                             user_id=user.id)
    if idem.replay is not None:
        return idem.replay

    def make_messages(feedback):
        # S-3 同类闭合：与 runner 同款隔离
        return _secure_messages(build_review_suggest_messages(
            record_title=rec.title, platform=rec.platform,
            metrics_history=history, review_note=rec.review_note or ""))

    try:
        try:
            result, meta = gw.generate_with_retry(
                "review_suggest", make_messages,
                validate_review_suggest_result,
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("review_suggest"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(
                status_code=503,
                detail="AI 复盘建议服务暂不可用（%s），请稍后再试" % e)

        texts: List[str] = []
        obs = result.get("observations") or []
        for x in obs:
            x = str(x).strip()
            texts.append(x if x.startswith("直接观察") else "直接观察：" + x)
        for x in result.get("suggestions") or []:
            x = str(x).strip()
            texts.append(x if x.startswith("建议") else "建议：" + x)
        full_text = "\n".join(texts)

        # 铁律：建议只存建议表，不触碰已确认内容与发布记录字段。
        sug = models.ReviewSuggestion(
            org_id=org_id, publish_record_id=rec.id,
            suggestion_text=full_text,
            status=models.SUGGESTION_PENDING,
            ai_meta={"prompt_version": get_prompt_version("review_suggest"),
                     "model": meta.get("model") if isinstance(meta, dict) else None,
                     "metrics_batches": len(history),
                     "has_review_note": bool(rec.review_note)},
            created_by=user.id)
        db.add(sug)
        db.commit()
        db.refresh(sug)
        resp = success({"suggestion": _suggestion_out(sug),
                        "reminder": "AI 建议仅供参考，采纳前请人工确认；"
                                    "建议不会自动修改任何已确认内容。"})
        return idem.finish(resp)
    except HTTPException:
        idem.abort()
        raise
    except Exception:
        idem.abort()
        raise


@router.post("/suggestions/{sug_id}/decision")
def decide_suggestion(sug_id: str, req: SuggestionDecision,
                      org_id: str = Query(...),
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """人工处理建议：采纳/忽略。只改状态，不写回任何业务数据。"""
    _require_review_perm(db, user, org_id, "publish:update", "处理复盘建议")
    sug = (db.query(models.ReviewSuggestion)
           .filter(models.ReviewSuggestion.id == sug_id,
                   models.ReviewSuggestion.org_id == org_id).first())
    if sug is None:
        raise BusinessError("复盘建议不存在",
                            code=error_codes.REVIEW_SUGGESTION_NOT_FOUND,
                            http_status=404)
    if req.decision == "adopted":
        sug.status = models.SUGGESTION_ADOPTED
    elif req.decision == "dismissed":
        sug.status = models.SUGGESTION_DISMISSED
    else:
        raise BusinessError("decision 仅支持 adopted/dismissed",
                            code=error_codes.INVALID_PARAM, http_status=400)
    db.commit()
    return success({"suggestion": _suggestion_out(sug),
                    "reminder": "已记录人工处理结果；未自动修改任何已确认内容。"})


# ---------- 效果看板 ----------

def _record_product_id(db: Session, rec: models.PublishRecord) -> Optional[str]:
    """经文案链条找产品：copy_version → copy_doc.product_id。"""
    cv_id = rec.copy_version_id
    pv_id = rec.platform_version_id
    if not cv_id and pv_id:
        pv = (db.query(models.PlatformVersion)
              .filter(models.PlatformVersion.id == pv_id).first())
        if pv is not None:
            cv_id = pv.copy_version_id
    if not cv_id:
        return None
    cv = (db.query(models.CopyVersion)
          .filter(models.CopyVersion.id == cv_id).first())
    if cv is None or not cv.doc_id:
        return None
    doc = (db.query(models.CopyDoc)
           .filter(models.CopyDoc.id == cv.doc_id).first())
    return doc.product_id if doc is not None else None


@router.get("/dashboard")
def dashboard(org_id: str = Query(...),
              work_id: Optional[str] = Query(default=None),
              product_id: Optional[str] = Query(default=None),
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """效果看板：作品/产品的历史发布数据列表与趋势。

    只展示已人工录入的数据；无数据 → has_data=false 空状态，
    绝不展示虚假图表。
    """
    _require_review_perm(db, user, org_id, "publish:read", "查看效果看板")
    q = (db.query(models.PublishRecord)
         .filter(models.PublishRecord.org_id == org_id,
                 models.PublishRecord.status.in_(
                     [models.PUBLISH_PUBLISHED, models.PUBLISH_REVIEWED])))
    if work_id:
        q = q.filter(models.PublishRecord.work_id == work_id)
    records = q.order_by(models.PublishRecord.published_at.desc()).all()

    items = []
    for rec in records:
        pid = _record_product_id(db, rec)
        if product_id and pid != product_id:
            continue
        batches = (db.query(models.EffectMetric)
                   .filter(models.EffectMetric.publish_record_id == rec.id,
                           models.EffectMetric.org_id == org_id)
                   .order_by(models.EffectMetric.recorded_at.asc()).all())
        latest = _metric_out(batches[-1]) if batches else None
        items.append({
            "record_id": rec.id,
            "title": rec.title,
            "platform": rec.platform,
            "status": rec.status,
            "published_at": (rec.published_at.isoformat()
                             if rec.published_at else None),
            "publish_url": rec.publish_url or "",
            "product_id": pid,
            "review_note": rec.review_note or "",
            "batches": len(batches),
            "latest": latest,
            "history": [_metric_out(b) for b in batches],
        })
    has_data = any(i["batches"] for i in items)
    return success({
        "records": items,
        "has_data": has_data,
        "empty_hint": ("" if has_data else
                       "暂无数据：还没有录入任何效果指标，"
                       "打开一条已发布记录即可人工录入。"),
    })
