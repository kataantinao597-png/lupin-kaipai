"""企业管理与商业化 API（S10，第十二阶段）。

- 用量报表：GET /api/v1/enterprise/{org_id}/usage/report
- 配额：GET/PUT /api/v1/enterprise/{org_id}/quotas
- Webhook：CRUD + 测试 + 投递记录
- 模板市场：CRUD + 版本管理（上架审核流留 TODO）
- 计费：价格配置 + 对账单 + 账单记录（价格由运营配置，不硬编码）

权限：读接口要求组织成员（org:read）；管理接口要求 owner/admin。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models
from ...auth.deps import get_current_user
from ...auth.database import get_db
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError, AuthError
from ...core.response import ApiResponse, success
from ...rbac import (ROLE_ADMIN, ROLE_OWNER, ROLE_RANK, get_membership,
                     require_org_permission)
from ...services import quota_service as quotas
from ...services import webhook_service as webhooks
from ...services import billing_service as billing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise", tags=["v1-enterprise"])


def _require_admin(ms: models.Membership, what: str = "管理企业配置") -> None:
    """050 二次验证：管理类操作仅 owner/admin。"""
    if ROLE_RANK.get(ms.role, 0) < ROLE_RANK[ROLE_ADMIN]:
        raise AuthError(f"权限不足，无法{what}", forbidden=True)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class QuotaSet(BaseModel):
    quota_type: str = Field(..., description="ai_calls_daily|storage_mb|members")
    limit_value: int = Field(..., description="限额；<=0 表示取消限制")


class QuotaOut(BaseModel):
    quota_type: str
    limit_value: int
    current_usage: int
    unlimited: bool


class WebhookCreate(BaseModel):
    url: str
    events: list


class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[list] = None
    is_active: Optional[bool] = None


class WebhookOut(BaseModel):
    id: str
    url: str
    events: list
    is_active: bool
    created_at: Optional[datetime] = None


class WebhookCreateOut(WebhookOut):
    secret: str = Field(..., description="签名密钥，仅创建时返回一次，请妥善保存")


class DeliveryOut(BaseModel):
    id: str
    event_type: str
    status: str
    attempts: int
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class TemplateCreate(BaseModel):
    name: str = Field(..., max_length=128)
    category: str = ""
    content: dict = Field(default_factory=dict)
    is_public: bool = False


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    content: Optional[dict] = None
    is_public: Optional[bool] = None


class TemplateOut(BaseModel):
    id: str
    name: str
    category: str
    version: int
    is_public: bool
    source: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    notice: Optional[str] = None


class TemplateVersionOut(BaseModel):
    version: int
    created_at: Optional[datetime] = None


class PriceSet(BaseModel):
    item: str
    unit_price_cents: int = Field(..., ge=0)
    currency: str = "CNY"
    note: str = ""


class PriceOut(BaseModel):
    item: str
    unit_price_cents: int
    currency: str
    version: int
    note: str


class BillingIssue(BaseModel):
    period: str = Field(..., description="YYYY-MM")


class BillingRecordOut(BaseModel):
    id: str
    period: str
    amount_cents: int
    currency: str
    status: str
    issued_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------

def _webhook_out(wh: models.Webhook) -> dict:
    try:
        events = json.loads(wh.events_json or "[]")
    except (ValueError, TypeError):
        events = []
    return {"id": wh.id, "url": wh.url, "events": events,
            "is_active": bool(wh.is_active), "created_at": wh.created_at}


def _get_webhook_or_404(db: Session, org_id: str, wh_id: str) -> models.Webhook:
    wh = db.query(models.Webhook).filter(
        models.Webhook.id == wh_id, models.Webhook.org_id == org_id).first()
    if wh is None:
        raise BusinessError("Webhook 不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    return wh


def _template_out(t: models.Template) -> dict:
    return {"id": t.id, "name": t.name, "category": t.category,
            "version": t.version, "is_public": bool(t.is_public),
            "source": t.source, "created_at": t.created_at,
            "updated_at": t.updated_at}


def _get_template_visible(db: Session, org_id: str,
                          tpl_id: str) -> models.Template:
    """组织内模板，或公开模板（跨组织可见）。"""
    t = db.query(models.Template).filter(models.Template.id == tpl_id).first()
    if t is None or not (t.org_id == org_id or t.is_public):
        raise BusinessError("模板不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    return t


# ---------------------------------------------------------------------------
# 1. 用量报表
# ---------------------------------------------------------------------------

@router.get("/{org_id}/usage/report", response_model=ApiResponse[dict])
def usage_report(
    org_id: str,
    date_from: Optional[str] = Query(None, description="ISO 日期，默认 30 天前"),
    date_to: Optional[str] = Query(None, description="ISO 日期，默认现在"),
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """组织用量报表。

    权限要求：组织成员（org:read）。

    请求示例：
        GET /api/v1/enterprise/{org_id}/usage/report?date_from=2026-10-01

    响应结构：标准信封，data 含 ai（调用数/token/费用/按模型与任务分布）、
    storage_mb（快照）、members（快照）、tasks（当前各状态数，快照）。

    说明：
    - ai_calls 经 ai_tasks 归属组织；同步调用（task_id 为空）无法归属，
      不计入组织报表（data.unattributed_note 说明）。
    - storage/tasks 为"当前快照"，非时间范围聚合。

    错误码：4000（日期格式非法）、4002（组织不存在/无权限，防枚举）。
    """
    now = datetime.now(timezone.utc)
    try:
        d_from = (_aware(datetime.fromisoformat(date_from)) if date_from
                  else now - timedelta(days=30))
        d_to = (_aware(datetime.fromisoformat(date_to)) if date_to
                else now)
    except ValueError:
        raise BusinessError("日期格式非法，应为 ISO 格式",
                            code=error_codes.INVALID_PARAM, http_status=400)
    if d_from > d_to:
        raise BusinessError("date_from 不能晚于 date_to",
                            code=error_codes.INVALID_PARAM, http_status=400)
    f_naive = d_from.replace(tzinfo=None)
    t_naive = d_to.replace(tzinfo=None)

    rows = (
        db.query(models.AICall.total_tokens, models.AICall.cost_cents,
                 models.AICall.model, models.AICall.task_type,
                 models.AICall.status)
        .join(models.AITask, models.AICall.task_id == models.AITask.id)
        .filter(models.AITask.org_id == org_id,
                models.AICall.created_at >= f_naive,
                models.AICall.created_at <= t_naive)
        .all()
    )
    by_model, by_task = {}, {}
    for tokens, cost, model, task_type, st in rows:
        m = by_model.setdefault(model or "unknown",
                                {"calls": 0, "tokens": 0, "cost_cents": 0})
        m["calls"] += 1
        m["tokens"] += tokens or 0
        m["cost_cents"] += cost or 0
        t = by_task.setdefault(task_type or "unknown", {"calls": 0})
        t["calls"] += 1
    storage_bytes = (
        db.query(func.coalesce(func.sum(func.length(models.Work.data_json)), 0))
        .filter(models.Work.org_id == org_id,
                models.Work.deleted_at.is_(None))
        .scalar() or 0
    )
    members = (
        db.query(func.count(models.Membership.id))
        .filter(models.Membership.org_id == org_id).scalar() or 0
    )
    task_states = dict(
        db.query(models.AITask.status, func.count(models.AITask.id))
        .filter(models.AITask.org_id == org_id)
        .group_by(models.AITask.status).all()
    )
    return success({
        "org_id": org_id,
        "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "ai": {
            "calls": len(rows),
            "total_tokens": sum(r[0] or 0 for r in rows),
            "cost_cents": sum(r[1] or 0 for r in rows),
            "by_model": by_model,
            "by_task_type": by_task,
        },
        "storage_mb": int(storage_bytes) // (1024 * 1024),
        "members": members,
        "tasks_by_status": task_states,
        "notes": [
            "storage_mb/members/tasks_by_status 为当前快照，非时间范围聚合。",
            "同步 AI 调用（task_id 为空）无法归属组织，未计入 ai 统计。",
            "cost_cents 为调用时按当时价格估算，仅供参考，不代表实际账单。",
        ],
    })


# ---------------------------------------------------------------------------
# 2. 配额
# ---------------------------------------------------------------------------

@router.get("/{org_id}/quotas", response_model=ApiResponse[list[QuotaOut]])
def list_quotas(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """查询组织配额及当前用量。

    权限要求：组织成员（org:read）。未配置的配额类型显示 unlimited=true。

    错误码：4002（组织不存在/无权限，防枚举）。
    """
    out = []
    for qt in sorted(quotas.QUOTA_TYPES):
        q = quotas.get_quota(db, org_id, qt)
        out.append({
            "quota_type": qt,
            "limit_value": q.limit_value if q else 0,
            "current_usage": quotas.current_usage(db, org_id, qt),
            "unlimited": q is None or (q.limit_value or 0) <= 0,
        })
    return success(out)


@router.put("/{org_id}/quotas", response_model=ApiResponse[QuotaOut])
def set_quota(
    org_id: str,
    req: QuotaSet,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """设置组织配额（仅 owner/admin）。

    权限要求：owner/admin。limit_value <= 0 表示取消限制。

    请求示例：
        PUT /api/v1/enterprise/{org_id}/quotas
        {"quota_type": "ai_calls_daily", "limit_value": 500}

    错误码：4000（未知配额类型）、4002（组织不存在/无权限）、
    4001/403（非管理员）。
    """
    _require_admin(ms, "调整配额")
    try:
        q = quotas.set_quota(db, org_id, req.quota_type, req.limit_value)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success({
        "quota_type": req.quota_type,
        "limit_value": q.limit_value if q else 0,
        "current_usage": quotas.current_usage(db, org_id, req.quota_type),
        "unlimited": q is None,
    })


# ---------------------------------------------------------------------------
# 3. Webhook
# ---------------------------------------------------------------------------

@router.post("/{org_id}/webhooks", response_model=ApiResponse[WebhookCreateOut],
             status_code=status.HTTP_201_CREATED)
def create_webhook(
    org_id: str,
    req: WebhookCreate,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """创建 Webhook 订阅（仅 owner/admin）。

    secret 仅在本次响应返回，请妥善保存；后续查询不再返回明文。
    URL 会做 SSRF 校验（拒绝内网/回环/保留地址）。

    请求示例：
        POST /api/v1/enterprise/{org_id}/webhooks
        {"url": "https://example.com/hook", "events": ["task.completed"]}

    错误码：4000（URL 不安全/事件非法）、4001/403（非管理员）。
    """
    _require_admin(ms, "管理 Webhook")
    try:
        url = webhooks._assert_url_safe(req.url)
        events = webhooks.parse_events(req.events)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    wh = models.Webhook(org_id=org_id, url=url,
                        events_json=json.dumps(events, ensure_ascii=False),
                        secret=webhooks.generate_secret(),
                        created_by=ms.user_id)
    db.add(wh)
    db.commit()
    db.refresh(wh)
    out = _webhook_out(wh)
    out["secret"] = wh.secret
    return success(out)


@router.get("/{org_id}/webhooks", response_model=ApiResponse[list[WebhookOut]])
def list_webhooks(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """Webhook 列表（不含 secret 明文）。权限要求：组织成员。"""
    rows = (db.query(models.Webhook)
            .filter(models.Webhook.org_id == org_id)
            .order_by(models.Webhook.created_at.desc()).all())
    return success([_webhook_out(w) for w in rows])


@router.patch("/{org_id}/webhooks/{wh_id}",
             response_model=ApiResponse[WebhookOut])
def update_webhook(
    org_id: str,
    wh_id: str,
    req: WebhookUpdate,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """更新 Webhook（仅 owner/admin）。"""
    _require_admin(ms, "管理 Webhook")
    wh = _get_webhook_or_404(db, org_id, wh_id)
    if req.url is not None:
        try:
            wh.url = webhooks._assert_url_safe(req.url)
        except ValueError as e:
            raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                                http_status=400)
    if req.events is not None:
        try:
            events = webhooks.parse_events(req.events)
        except ValueError as e:
            raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                                http_status=400)
        wh.events_json = json.dumps(events, ensure_ascii=False)
    if req.is_active is not None:
        wh.is_active = bool(req.is_active)
    db.commit()
    db.refresh(wh)
    return success(_webhook_out(wh))


@router.delete("/{org_id}/webhooks/{wh_id}", status_code=204)
def delete_webhook(
    org_id: str,
    wh_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """删除 Webhook（仅 owner/admin），投递记录级联删除。"""
    _require_admin(ms, "管理 Webhook")
    wh = _get_webhook_or_404(db, org_id, wh_id)
    db.delete(wh)
    db.commit()
    return None


@router.post("/{org_id}/webhooks/{wh_id}/test",
            response_model=ApiResponse[dict])
def test_webhook(
    org_id: str,
    wh_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """同步测试投递一次（仅 owner/admin），返回投递结果，不建投递记录。"""
    _require_admin(ms, "管理 Webhook")
    wh = _get_webhook_or_404(db, org_id, wh_id)
    payload = {"event": "webhook.test", "org_id": org_id,
               "occurred_at": datetime.now(timezone.utc).isoformat(),
               "data": {"webhook_id": wh.id}}
    try:
        webhooks._post_delivery(wh.url, wh.secret, "webhook.test",
                                "test", payload)
    except Exception as e:  # noqa: BLE001
        return success({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return success({"ok": True})


@router.get("/{org_id}/webhooks/{wh_id}/deliveries",
            response_model=ApiResponse[list[DeliveryOut]])
def list_deliveries(
    org_id: str,
    wh_id: str,
    limit: int = Query(50, ge=1, le=200),
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """投递记录（含死信）。权限要求：组织成员。"""
    _get_webhook_or_404(db, org_id, wh_id)
    rows = (db.query(models.WebhookDelivery)
            .filter(models.WebhookDelivery.webhook_id == wh_id)
            .order_by(models.WebhookDelivery.created_at.desc())
            .limit(limit).all())
    return success([{
        "id": d.id, "event_type": d.event_type, "status": d.status,
        "attempts": d.attempts, "last_error": d.last_error,
        "created_at": d.created_at, "delivered_at": d.delivered_at,
    } for d in rows])


# ---------------------------------------------------------------------------
# 4. 模板市场
# ---------------------------------------------------------------------------

@router.post("/{org_id}/templates", response_model=ApiResponse[TemplateOut],
             status_code=status.HTTP_201_CREATED)
def create_template(
    org_id: str,
    req: TemplateCreate,
    ms: models.Membership = Depends(require_org_permission("work:create")),
    db: Session = Depends(get_db),
):
    """创建模板（需 work:create）。上架审核流留 TODO：is_public 当前由
    owner/admin 直接控制，普通成员创建公开模板会被强制为私有。

    请求示例：
        POST /api/v1/enterprise/{org_id}/templates
        {"name": "农产品口播模板", "category": "口播",
         "content": {"shots": []}, "is_public": false}
    """
    name = req.name.strip()
    if not name:
        raise BusinessError("模板名称不能为空", code=error_codes.INVALID_PARAM,
                            http_status=400)
    is_public = bool(req.is_public)
    if is_public and ROLE_RANK.get(ms.role, 0) < ROLE_RANK[ROLE_ADMIN]:
        # TODO(S10): 上架审核流——当前直接降级为私有
        is_public = False
    t = models.Template(org_id=org_id, name=name,
                        category=req.category.strip(),
                        content_json=json.dumps(req.content or {},
                                                ensure_ascii=False),
                        version=1, is_public=is_public, source="custom",
                        created_by=ms.user_id)
    db.add(t)
    db.flush()
    db.add(models.TemplateVersion(template_id=t.id, version=1,
                                  content_json=t.content_json,
                                  created_by=ms.user_id))
    db.commit()
    db.refresh(t)
    out = _template_out(t)
    if req.is_public and not is_public:
        out["notice"] = "公开模板需管理员审核，已暂存为私有（TODO：上架审核流）"
    return success(out)


@router.get("/{org_id}/templates", response_model=ApiResponse[list[TemplateOut]])
def list_templates(
    org_id: str,
    category: Optional[str] = Query(None),
    public_only: bool = Query(False, description="只看公开模板（含其他组织）"),
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """模板列表。public_only=true 时返回所有组织的公开模板。"""
    q = db.query(models.Template)
    if public_only:
        q = q.filter(models.Template.is_public.is_(True))
    else:
        q = q.filter(models.Template.org_id == org_id)
    if category:
        q = q.filter(models.Template.category == category)
    rows = q.order_by(models.Template.updated_at.desc()).limit(200).all()
    return success([_template_out(t) for t in rows])


@router.get("/{org_id}/templates/{tpl_id}",
           response_model=ApiResponse[dict])
def get_template(
    org_id: str,
    tpl_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """模板详情（含当前版本内容）。公开模板跨组织可见。"""
    t = _get_template_visible(db, org_id, tpl_id)
    out = _template_out(t)
    out["content"] = json.loads(t.content_json or "{}")
    return success(out)


@router.patch("/{org_id}/templates/{tpl_id}",
             response_model=ApiResponse[TemplateOut])
def update_template(
    org_id: str,
    tpl_id: str,
    req: TemplateUpdate,
    ms: models.Membership = Depends(require_org_permission("work:create")),
    db: Session = Depends(get_db),
):
    """更新模板：内容变更时 version+1 并留快照（需 work:create）。"""
    t = db.query(models.Template).filter(
        models.Template.id == tpl_id,
        models.Template.org_id == org_id).first()
    if t is None:
        raise BusinessError("模板不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    if req.name is not None:
        if not req.name.strip():
            raise BusinessError("模板名称不能为空",
                                code=error_codes.INVALID_PARAM, http_status=400)
        t.name = req.name.strip()
    if req.category is not None:
        t.category = req.category.strip()
    if req.content is not None:
        t.content_json = json.dumps(req.content, ensure_ascii=False)
        t.version = (t.version or 1) + 1
        db.add(models.TemplateVersion(template_id=t.id, version=t.version,
                                      content_json=t.content_json,
                                      created_by=ms.user_id))
    if req.is_public is not None:
        if req.is_public and ROLE_RANK.get(ms.role, 0) < ROLE_RANK[ROLE_ADMIN]:
            raise AuthError("公开模板需管理员操作", forbidden=True)
        t.is_public = bool(req.is_public)
    db.commit()
    db.refresh(t)
    return success(_template_out(t))


@router.delete("/{org_id}/templates/{tpl_id}", status_code=204)
def delete_template(
    org_id: str,
    tpl_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """删除模板（仅 owner/admin），版本快照级联删除。"""
    _require_admin(ms, "删除模板")
    t = db.query(models.Template).filter(
        models.Template.id == tpl_id,
        models.Template.org_id == org_id).first()
    if t is None:
        raise BusinessError("模板不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    db.delete(t)
    db.commit()
    return None


@router.get("/{org_id}/templates/{tpl_id}/versions",
           response_model=ApiResponse[list[TemplateVersionOut]])
def list_template_versions(
    org_id: str,
    tpl_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """模板版本历史。"""
    _get_template_visible(db, org_id, tpl_id)
    rows = (db.query(models.TemplateVersion)
            .filter(models.TemplateVersion.template_id == tpl_id)
            .order_by(models.TemplateVersion.version.desc()).all())
    return success([{"version": v.version, "created_at": v.created_at}
                    for v in rows])


# ---------------------------------------------------------------------------
# 5. 计费
# ---------------------------------------------------------------------------

@router.get("/{org_id}/billing/prices",
           response_model=ApiResponse[list[PriceOut]])
def list_prices(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """当前价格表（最新版本）。权限要求：组织成员。"""
    prices = billing.current_prices(db)
    return success([{
        "item": item,
        "unit_price_cents": prices[item].unit_price_cents,
        "currency": prices[item].currency,
        "version": prices[item].version,
        "note": prices[item].note,
    } for item in billing.PRICE_ITEMS])


@router.put("/{org_id}/billing/prices", response_model=ApiResponse[PriceOut])
def set_price(
    org_id: str,
    req: PriceSet,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """设置价格（仅 owner/admin）：新增版本，不改历史。

    注意：价格为平台全局配置，此处经组织管理员操作；上线前必须由运营
    配置真实价格，0 占位仅用于联调。
    """
    _require_admin(ms, "配置价格")
    try:
        row = billing.set_price(db, req.item, req.unit_price_cents,
                                req.currency, req.note)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success({"item": row.item, "unit_price_cents": row.unit_price_cents,
                    "currency": row.currency, "version": row.version,
                    "note": row.note})


@router.post("/{org_id}/billing/prices/seed",
            response_model=ApiResponse[list[PriceOut]])
def seed_prices(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """补齐 0 占位价格（仅 owner/admin，幂等）。"""
    _require_admin(ms, "配置价格")
    billing.ensure_price_seed(db)
    prices = billing.current_prices(db)
    return success([{
        "item": item,
        "unit_price_cents": prices[item].unit_price_cents,
        "currency": prices[item].currency,
        "version": prices[item].version,
        "note": prices[item].note,
    } for item in billing.PRICE_ITEMS])


@router.get("/{org_id}/billing/statement", response_model=ApiResponse[dict])
def billing_statement(
    org_id: str,
    period: Optional[str] = Query(None, description="YYYY-MM，默认当月"),
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """对账单（不落库）：用量明细 + 按当前价格估算的金额。

    权限要求：组织成员。prices_unset=true 表示价格仍为 0 占位，
    金额为 0，仅供联调。

    错误码：4000（period 格式非法）。
    """
    if not period:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        stmt = billing.build_statement(db, org_id, period)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success(stmt)


@router.post("/{org_id}/billing/records",
            response_model=ApiResponse[BillingRecordOut],
            status_code=status.HTTP_201_CREATED)
def issue_billing_record(
    org_id: str,
    req: BillingIssue,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """开出账单记录（仅 owner/admin）。同一组织+周期只允许一张。"""
    _require_admin(ms, "开账单")
    try:
        rec = billing.issue_record(db, org_id, req.period, ms.user_id)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    if rec is None:
        raise BusinessError(f"{req.period} 的账单已存在",
                            code=error_codes.CONFLICT, http_status=409)
    return success({"id": rec.id, "period": rec.period,
                    "amount_cents": rec.amount_cents, "currency": rec.currency,
                    "status": rec.status, "issued_at": rec.issued_at})


@router.get("/{org_id}/billing/records",
           response_model=ApiResponse[list[BillingRecordOut]])
def list_billing_records(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    """账单记录列表。权限要求：组织成员。"""
    rows = (db.query(models.BillingRecord)
            .filter(models.BillingRecord.org_id == org_id)
            .order_by(models.BillingRecord.period.desc()).all())
    return success([{"id": r.id, "period": r.period,
                     "amount_cents": r.amount_cents, "currency": r.currency,
                     "status": r.status, "issued_at": r.issued_at}
                    for r in rows])
