"""媒体资产 API（S8.1/S8.2）。

- POST   /api/v1/assets/upload-intents   创建一次性上传授权（需 asset:upload）
- POST   /api/v1/assets/upload           凭 token 上传文件（multipart，需 asset:upload）
- GET    /api/v1/assets?org_id=&work_id=  资产列表（需 asset:read）
- GET    /api/v1/assets/{id}?org_id=      资产详情（含元数据，需 asset:read）
- DELETE /api/v1/assets/{id}?org_id=      删除资产（需 asset:delete）
- GET    /api/v1/assets/{id}/download?org_id=  签发 5 分钟签名下载 URL（需 asset:read）
- GET    /api/v1/assets/{id}/raw?exp=&sig=     验签后发文件（免登录，签名即凭证）

S8.1：业务库只存元数据 + 对象 key；文件走 StorageBackend（默认本地）。
上传校验：魔数识别 + 大小限制（图片10MB/视频200MB/压缩包50MB）+ 可选 sha256。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...core.response import ApiResponse, success
from ...media import signing, validation
from ...media.storage import get_storage
from ...rbac import assert_can, get_membership, has_permission

router = APIRouter(prefix="/assets", tags=["v1-assets"])

INTENT_TTL = timedelta(minutes=15)  # 上传意图有效期


# ---------- schemas ----------

class UploadIntentReq(BaseModel):
    org_id: str
    work_id: str
    filename: str = Field(max_length=256)
    mime: str = Field(default="", max_length=128)   # 声明类型（上传时再验魔数）
    size: int = Field(gt=0, description="预期字节数")
    sha256: Optional[str] = Field(default=None, max_length=64,
                                  description="可选：前端算好的哈希，上传后比对")


class UploadIntentOut(BaseModel):
    upload_token: str
    key: str
    expires_at: str
    max_size_bytes: int


class AssetOut(BaseModel):
    id: str
    org_id: str
    work_id: str
    key: str
    filename: str
    mime: str
    size: int
    sha256: Optional[str]
    status: str
    meta: dict = Field(default_factory=dict)
    created_at: Optional[str] = None


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int


# ---------- helpers ----------

def _require_asset_perm(db: Session, user: User, org_id: str,
                        action: str, what: str):
    """取成员身份并校验权限；无权一律 404（防枚举）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite 读回的是 naive datetime，统一按 UTC 处理（与 orgs.py/works.py 一致）。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_filename(name: str) -> str:
    """展示用文件名：取 basename，去路径分隔符与控制字符，保留中文。"""
    base = os.path.basename(name or "file")
    base = re.sub(r"[\x00-\x1f\x7f/\\]", "_", base).strip("._") or "file"
    return base[:128]


def _safe_key_name(name: str) -> str:
    """对象 key 用名：存储层 key 白名单只认 ASCII，中文转下划线。"""
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "file"
    return base[:64]


def _asset_out(a: models.Asset) -> AssetOut:
    try:
        meta = json.loads(a.meta_json or "{}")
    except (ValueError, TypeError):
        meta = {}
    return AssetOut(
        id=a.id, org_id=a.org_id, work_id=a.work_id, key=a.key,
        filename=a.filename or "", mime=a.mime or "", size=a.size or 0,
        sha256=a.sha256, status=a.status or "",
        meta=meta if isinstance(meta, dict) else {},
        created_at=a.created_at.isoformat() if a.created_at else None,
    )


def _get_asset(db: Session, asset_id: str, org_id: str) -> models.Asset:
    a = (db.query(models.Asset)
         .filter(models.Asset.id == asset_id,
                 models.Asset.org_id == org_id,
                 models.Asset.status != models.ASSET_STATUS_DELETED)
         .first())
    if a is None:
        raise BusinessError("资产不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    return a


# ---------- 上传意图 ----------

@router.post("/upload-intents", response_model=ApiResponse[UploadIntentOut],
             status_code=status.HTTP_201_CREATED)
def create_upload_intent(
    req: UploadIntentReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S8.1：创建一次性上传授权。

    校验：作品归属组织、声明类型对应的大小上限。
    返回 upload_token（一次有效，15 分钟过期）+ 预分配的对象 key。
    """
    _require_asset_perm(db, user, req.org_id, "asset:upload", "上传媒体文件")
    # 作品必须属于该组织（防跨组织挂载）
    work = (db.query(models.Work)
            .filter(models.Work.id == req.work_id,
                    models.Work.org_id == req.org_id,
                    models.Work.deleted_at.is_(None))
            .first())
    if work is None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    category = validation.category_of_mime(req.mime)
    max_size = (validation.SIZE_LIMITS[category] if category
                else max(validation.SIZE_LIMITS.values()))
    if req.size > max_size:
        raise BusinessError(
            f"文件过大：声明 {req.size} 字节，上限 {max_size} 字节",
            code=error_codes.INVALID_PARAM, http_status=400)
    if req.sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", req.sha256):
        raise BusinessError("sha256 格式非法（应为 64 位 hex）",
                            code=error_codes.INVALID_PARAM, http_status=400)

    safe_name = _safe_filename(req.filename)
    key = f"{req.org_id}/{req.work_id}/{uuid.uuid4().hex}_{_safe_key_name(req.filename)}"
    now = datetime.now(timezone.utc)
    intent = models.UploadIntent(
        token=secrets.token_urlsafe(32),
        org_id=req.org_id, work_id=req.work_id, key=key,
        filename=safe_name, mime=req.mime or "",
        expected_size=req.size, expected_sha256=(req.sha256 or "").lower() or None,
        created_by=user.id, expires_at=now + INTENT_TTL,
    )
    db.add(intent)
    db.commit()
    return success(UploadIntentOut(
        upload_token=intent.token, key=key,
        expires_at=intent.expires_at.isoformat(), max_size_bytes=max_size,
    ).model_dump())


# ---------- 上传文件 ----------

@router.post("/upload", response_model=ApiResponse[AssetOut],
             status_code=status.HTTP_201_CREATED)
def upload_file(
    upload_token: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S8.1/S8.2：凭一次性 token 上传文件。

    流程：验 token（一次有效、未过期）→ 读字节（带上限）→ 魔数+大小校验 →
    可选 sha256 比对 → 入库存储 → 建资产（ready）+ 媒体处理任务（pending）。
    校验失败返回 400，不建资产。
    """
    now = datetime.now(timezone.utc)
    intent = db.get(models.UploadIntent, upload_token)
    expires_at = _as_aware(intent.expires_at) if intent else None
    if intent is None or intent.used or expires_at is None \
            or expires_at <= now:
        raise BusinessError("上传授权无效或已过期",
                            code=error_codes.INVALID_PARAM, http_status=400)
    _require_asset_perm(db, user, intent.org_id, "asset:upload", "上传媒体文件")

    # 读字节：上限 = 意图声明的上限类别（防内存爆炸）
    category = validation.category_of_mime(intent.mime)
    cap = (validation.SIZE_LIMITS[category] if category
           else max(validation.SIZE_LIMITS.values())) + 1024
    data = file.file.read(cap + 1)
    if len(data) > cap:
        raise BusinessError("文件超过大小上限",
                            code=error_codes.INVALID_PARAM, http_status=400)
    if not data:
        raise BusinessError("空文件",
                            code=error_codes.INVALID_PARAM, http_status=400)

    # 魔数 + 大小校验（声明 mime 不符也拒绝）
    try:
        mime, _cat = validation.validate_upload(data, intent.mime)
    except validation.MediaValidationError as e:
        raise BusinessError(f"文件校验失败：{e}",
                            code=error_codes.INVALID_PARAM,
                            http_status=400) from e

    # 可选 sha256 比对
    digest = hashlib.sha256(data).hexdigest()
    if intent.expected_sha256 and intent.expected_sha256 != digest:
        raise BusinessError("文件哈希与声明不一致",
                            code=error_codes.INVALID_PARAM, http_status=400)

    # 入库 + 建资产 + 建处理任务（同一事务）。
    # V2.1 一致性：commit（含 flush）失败时回滚并清理已入库的存储文件，
    # 避免"DB 无记录但存储有文件"的孤儿；清理失败不掩盖原始异常。
    get_storage().put(intent.key, data)
    intent.used = True
    try:
        asset = models.Asset(
            org_id=intent.org_id, work_id=intent.work_id, key=intent.key,
            filename=intent.filename, mime=mime, size=len(data), sha256=digest,
            status=models.ASSET_STATUS_READY, created_by=user.id,
        )
        db.add(asset)
        db.flush()
        task = models.MediaTask(asset_id=asset.id)
        db.add(task)
        db.commit()
    except Exception:
        db.rollback()
        try:
            get_storage().delete(intent.key)
        except Exception:
            pass
        raise
    db.refresh(asset)
    return success(_asset_out(asset).model_dump())


# ---------- 查询 / 删除 ----------

@router.get("", response_model=ApiResponse[list[AssetOut]])
def list_assets(
    org_id: str = Query(...),
    work_id: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """资产列表（需 asset:read）。"""
    _require_asset_perm(db, user, org_id, "asset:read", "查看媒体资产")
    q = (db.query(models.Asset)
         .filter(models.Asset.org_id == org_id,
                 models.Asset.status != models.ASSET_STATUS_DELETED))
    if work_id:
        q = q.filter(models.Asset.work_id == work_id)
    rows = q.order_by(models.Asset.created_at.desc()).limit(200).all()
    return success([_asset_out(a).model_dump() for a in rows])


@router.get("/{asset_id}", response_model=ApiResponse[AssetOut])
def get_asset(
    asset_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """资产详情（含处理任务提取的元数据，需 asset:read）。"""
    _require_asset_perm(db, user, org_id, "asset:read", "查看媒体资产")
    return success(_asset_out(_get_asset(db, asset_id, org_id)).model_dump())


@router.delete("/{asset_id}", response_model=ApiResponse[dict])
def delete_asset(
    asset_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除资产：清存储文件 + 状态置 deleted（需 asset:delete）。"""
    _require_asset_perm(db, user, org_id, "asset:delete", "删除媒体资产")
    asset = _get_asset(db, asset_id, org_id)
    try:
        get_storage().delete(asset.key)
    except Exception:  # noqa: BLE001 — 文件删失败不阻断状态标记
        pass
    asset.status = models.ASSET_STATUS_DELETED
    db.commit()
    return success({"id": asset_id, "status": "deleted"})


# ---------- 签名下载 ----------

@router.get("/{asset_id}/download", response_model=ApiResponse[DownloadUrlOut])
def download_url(
    asset_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S8.1：签发 5 分钟有效的签名下载 URL（需 asset:read）。

    不暴露永久路径；raw 端点验签后发文件，签名即凭证。
    """
    _require_asset_perm(db, user, org_id, "asset:read", "下载媒体文件")
    _get_asset(db, asset_id, org_id)
    params = signing.make_signed_params(asset_id)
    url = (f"/api/v1/assets/{asset_id}/raw"
           f"?exp={params['exp']}&sig={params['sig']}")
    return success(DownloadUrlOut(url=url,
                                  expires_in=signing.SIGN_TTL_SECONDS
                                  ).model_dump())


@router.get("/{asset_id}/raw")
def download_raw(
    asset_id: str,
    exp: int = Query(...),
    sig: str = Query(...),
    db: Session = Depends(get_db),
):
    """验签后发送文件（免登录）。签名错误/过期 → 403。"""
    if not signing.verify(asset_id, exp, sig):
        raise BusinessError("下载链接无效或已过期",
                            code=error_codes.PERMISSION_DENIED,
                            http_status=403)
    asset = db.get(models.Asset, asset_id)
    if asset is None or asset.status != models.ASSET_STATUS_READY:
        raise BusinessError("资产不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    storage = get_storage()
    # V2.1 一致性：DB 有记录但物理文件丢失 → 410（不要 500）。
    # 注意 LocalStorageBackend.local_path 在文件缺失时返回 None，
    # 不可用 local_path 是否为 None 区分"本地缺文件"与"S3 后端"。
    if not storage.exists(asset.key):
        raise BusinessError("文件已丢失，请重新上传",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=410)
    path = storage.local_path(asset.key)
    if path is None:
        # S3 后端：重定向到预签名 URL
        presigned = getattr(storage, "presigned_url", None)
        if presigned is None:
            raise BusinessError("当前存储后端不支持直接下载",
                                code=error_codes.SYSTEM_ERROR, http_status=500)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=presigned(asset.key, expires_in=300))
    return FileResponse(path, media_type=asset.mime or None,
                        filename=asset.filename or None)
