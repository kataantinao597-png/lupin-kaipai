"""集成用 API Key（服务账号）管理（Phase 9-4）。

027：标准响应信封；028：业务码。

- POST   /api/v1/api-keys          创建（仅 owner/admin；明文只返回一次）
- GET    /api/v1/api-keys?org_id= 列表（不含 hash）
- GET    /api/v1/api-keys/me       用 X-API-Key 自检（返回 org/scopes）
- DELETE /api/v1/api-keys/{key_id} 撤销（软撤销，保留审计痕迹）

注意：FastAPI 按声明顺序匹配，/me 必须在 /{key_id} 之前。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import assert_can, get_membership, has_permission
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...core.response import ApiResponse, success
from ...security.api_keys import generate_api_key, touch_api_key, verify_api_key
from ...security.audit import client_ip, log_audit

router = APIRouter(prefix="/api-keys", tags=["v1-api-keys"])

# 允许授予的 scope 白名单（最小权限：默认只读 + AI 调用）
ALLOWED_SCOPES = frozenset({
    "works:read", "works:write",
    "facts:read", "ai:use", "export:read",
})


class ApiKeyCreate(BaseModel):
    org_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyOut(BaseModel):
    id: str
    org_id: str
    name: str
    key_prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_by: str
    created_at: datetime


def _out(rec: models.ApiKey) -> dict:
    return ApiKeyOut(
        id=rec.id, org_id=rec.org_id, name=rec.name,
        key_prefix=rec.key_prefix, scopes=rec.scopes or [],
        expires_at=rec.expires_at, last_used_at=rec.last_used_at,
        revoked_at=rec.revoked_at, created_by=rec.created_by,
        created_at=rec.created_at,
    ).model_dump(mode="json")


def _require_key_admin(db, user_id: str, org_id: str):
    """API Key 管理是组织级敏感操作，仅 owner/admin。

    org_id 来自 body/query（非路径参数），这里手动校验，
    保持与 require_org_permission 一致的 404 防枚举语义。
    """
    org = db.query(models.Organization).filter(
        models.Organization.id == org_id).first()
    if org is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    ms = get_membership(db, user_id, org_id)
    if ms is None or not has_permission(ms.role, "org:config:model"):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, "org:config:model", "管理 API Key")
    return ms


@router.post("", response_model=ApiResponse[dict])
def create_api_key(
    body: ApiKeyCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建服务账号 Key。明文 api_key 只在本次响应返回，服务端不存明文。"""
    _require_key_admin(db, user.id, body.org_id)
    bad = [s for s in body.scopes if s not in ALLOWED_SCOPES]
    if bad:
        raise BusinessError(f"不支持的 scope: {bad}，可选 {sorted(ALLOWED_SCOPES)}",
                            code=error_codes.INVALID_PARAM, http_status=422)
    plaintext, digest = generate_api_key()
    rec = models.ApiKey(
        org_id=body.org_id,
        name=body.name.strip(),
        key_hash=digest,
        key_prefix=plaintext[:10],
        scopes=sorted(set(body.scopes)),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
                    if body.expires_in_days else None),
        created_by=user.id,
    )
    db.add(rec)
    log_audit(db, action="api_key.create", org_id=body.org_id,
              user_id=user.id, object_type="api_key", object_id=rec.id,
              detail={"name": rec.name, "scopes": rec.scopes,
                      "key_prefix": rec.key_prefix},
              ip=client_ip(request))
    db.commit()
    data = _out(rec)
    data["api_key"] = plaintext  # 仅此一次
    data["_warning"] = "明文 Key 只显示一次，请立即保存；服务端不存储明文"
    return success(data)


@router.get("", response_model=ApiResponse[dict])
def list_api_keys(
    org_id: str = Query(min_length=1),
    include_revoked: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出组织的 API Key（不含 hash，明文永不返回）。"""
    _require_key_admin(db, user.id, org_id)
    q = db.query(models.ApiKey).filter(models.ApiKey.org_id == org_id)
    if not include_revoked:
        q = q.filter(models.ApiKey.revoked_at.is_(None))
    recs = q.order_by(models.ApiKey.created_at.desc()).all()
    return success({"items": [_out(r) for r in recs], "total": len(recs)})


@router.get("/me", response_model=ApiResponse[dict])
def api_key_me(request: Request, db: Session = Depends(get_db)):
    """用 X-API-Key 自检：验证 Key 有效并返回所属组织与 scope。"""
    raw = request.headers.get("X-API-Key", "").strip()
    rec = verify_api_key(db, raw)
    if rec is None:
        raise AuthError("API Key 无效、已撤销或已过期",
                        code=error_codes.UNAUTHENTICATED, http_status=401)
    touch_api_key(db, rec)
    db.commit()
    return success({"org_id": rec.org_id, "name": rec.name,
                    "scopes": rec.scopes or [],
                    "key_prefix": rec.key_prefix})


@router.delete("/{key_id}", response_model=ApiResponse[dict])
def revoke_api_key(
    key_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    # 注意：require_org_permission 需要路径参数 org_id；Key 归属组织从记录查，
    # 这里先按记录查组织再验权限。
    db: Session = Depends(get_db),
):
    """撤销 API Key（软撤销，保留记录供审计）。"""
    rec = db.query(models.ApiKey).filter(models.ApiKey.id == key_id).first()
    if rec is None or rec.revoked_at is not None:
        raise BusinessError("API Key 不存在或已撤销",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    ms = get_membership(db, user.id, rec.org_id)
    if ms is None or not has_permission(ms.role, "org:config:model"):
        # 404 防枚举（047 同款）
        raise BusinessError("API Key 不存在或已撤销",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=404)
    assert_can(ms.role, "org:config:model", "管理 API Key")
    rec.revoked_at = datetime.now(timezone.utc)
    log_audit(db, action="api_key.revoke", org_id=rec.org_id,
              user_id=user.id, object_type="api_key", object_id=rec.id,
              detail={"name": rec.name, "key_prefix": rec.key_prefix},
              ip=client_ip(request))
    db.commit()
    return success({"id": rec.id, "revoked": True})
