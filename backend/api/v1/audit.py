"""审计日志查询（Phase 9-5）。

- GET /api/v1/audit/events?org_id=...  仅 owner/admin 可查（敏感操作记录）

027：标准响应信封；分页 page/page_size（上限 200）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import assert_can, get_membership, has_permission
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success

router = APIRouter(prefix="/audit", tags=["v1-audit"])


class AuditEventOut(BaseModel):
    id: str
    org_id: str | None
    user_id: str | None
    action: str
    object_type: str | None
    object_id: str | None
    detail_json: dict | None
    ip: str | None
    created_at: datetime


@router.get("/events", response_model=ApiResponse[dict])
def list_audit_events(
    org_id: str = Query(min_length=1),
    action: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询审计事件。仅组织 owner/admin 可见；无权返回 404（防枚举）。"""
    org = db.query(models.Organization).filter(
        models.Organization.id == org_id).first()
    if org is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    ms = get_membership(db, user.id, org_id)
    if ms is None or ms.role not in ("owner", "admin"):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, "org:read", "查看审计日志")

    q = (db.query(models.AuditEvent)
         .filter(models.AuditEvent.org_id == org_id))
    if action:
        q = q.filter(models.AuditEvent.action == action)
    total = q.count()
    rows = (q.order_by(models.AuditEvent.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    items = [AuditEventOut(
        id=r.id, org_id=r.org_id, user_id=r.user_id, action=r.action,
        object_type=r.object_type, object_id=r.object_id,
        detail_json=r.detail_json, ip=r.ip,
        created_at=r.created_at).model_dump(mode="json") for r in rows]
    return success({"items": items, "total": total,
                    "page": page, "page_size": page_size})
