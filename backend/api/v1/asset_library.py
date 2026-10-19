"""跨项目素材库 API（V3.0 G）。

引用为主，不复制文件：
- GET    /api/v1/asset-library/search?org_id=&q=&mime_type=&work_id=  跨项目检索（需 asset:read）
- GET    /api/v1/asset-library/assets/{asset_id}?org_id=              素材详情：来源项目 + 引用项目 + 引用计数（需 asset:read）
- POST   /api/v1/asset-library/refs                                  引用到指定项目（需 asset:upload）
- DELETE /api/v1/asset-library/refs/{ref_id}?org_id=                 解除引用 ≠ 删除实体（需 asset:delete）

删除安全：资产实体 DELETE（assets.py）前由 API 层检查引用计数，
仍被引用时返回 409/ASSET_HAS_REFS；解除全部引用后才能删除实体。
本模块不改 StorageBackend、不改 assets 上传链路。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import success
from ...rbac import assert_can, get_membership, has_permission

router = APIRouter(prefix="/asset-library", tags=["v1-asset-library"])

MIME_TYPES = ("image", "video", "audio", "other")
MIME_PREFIX = {"image": "image/", "video": "video/", "audio": "audio/"}


# ---------- schemas ----------

class RefCreateReq(BaseModel):
    org_id: str
    asset_id: str
    dest_work_id: str = Field(description="要引用到的目标项目 id")


class AssetCard(BaseModel):
    id: str
    filename: str
    mime: str
    size: int
    created_at: Optional[str] = None
    work_id: str
    work_title: str
    ref_count: int = 0


class RefOut(BaseModel):
    id: str
    org_id: str
    asset_id: str
    source_work_id: Optional[str] = None
    source_work_title: str = ""
    dest_work_id: str
    dest_work_title: str = ""
    created_by: str
    created_at: Optional[str] = None


# ---------- helpers ----------

def _require_lib_perm(db: Session, user: User, org_id: str,
                      action: str, what: str):
    """取成员身份并校验权限；无权一律 404（防枚举，与 assets.py 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _alive_asset(db: Session, asset_id: str, org_id: str) -> models.Asset:
    a = (db.query(models.Asset)
         .filter(models.Asset.id == asset_id,
                 models.Asset.org_id == org_id,
                 models.Asset.status != models.ASSET_STATUS_DELETED)
         .first())
    if a is None:
        raise BusinessError("素材不存在",
                            code=error_codes.RESOURCE_NOT_FOUND, http_status=404)
    return a


def _alive_work(db: Session, work_id: str, org_id: str) -> models.Work:
    w = (db.query(models.Work)
         .filter(models.Work.id == work_id,
                 models.Work.org_id == org_id,
                 models.Work.deleted_at.is_(None))
         .first())
    if w is None:
        raise BusinessError("项目不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    return w


def _work_titles(db: Session, org_id: str) -> dict:
    """项目 id → 标题（含已删除项目，引用历史展示用）。"""
    rows = (db.query(models.Work.id, models.Work.title)
            .filter(models.Work.org_id == org_id).all())
    return {r[0]: (r[1] or "") for r in rows}


def _ref_counts(db: Session, org_id: str, asset_ids: List[str]) -> dict:
    if not asset_ids:
        return {}
    rows = (db.query(models.AssetRef.asset_id,
                    func.count(models.AssetRef.id))
            .filter(models.AssetRef.org_id == org_id,
                    models.AssetRef.asset_id.in_(asset_ids))
            .group_by(models.AssetRef.asset_id).all())
    return {r[0]: r[1] for r in rows}


def ref_count_of(db: Session, org_id: str, asset_id: str) -> int:
    return (db.query(func.count(models.AssetRef.id))
            .filter(models.AssetRef.org_id == org_id,
                    models.AssetRef.asset_id == asset_id)
            .scalar() or 0)


def _ref_out(db: Session, ref: models.AssetRef, titles: dict) -> RefOut:
    return RefOut(
        id=ref.id, org_id=ref.org_id, asset_id=ref.asset_id,
        source_work_id=ref.source_work_id,
        source_work_title=titles.get(ref.source_work_id or "", ""),
        dest_work_id=ref.dest_work_id,
        dest_work_title=titles.get(ref.dest_work_id, ""),
        created_by=ref.created_by or "",
        created_at=ref.created_at.isoformat() if ref.created_at else None,
    )


# ---------- 跨项目检索 ----------

@router.get("/search", response_model=dict)
def search_library(
    org_id: str = Query(...),
    q: Optional[str] = Query(default=None, description="素材名称关键字"),
    mime_type: Optional[str] = Query(default=None,
                                     description="image|video|audio|other"),
    work_id: Optional[str] = Query(default=None, description="按所属项目过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """跨项目检索全库素材（org 隔离，不含已删除资产，需 asset:read）。"""
    _require_lib_perm(db, user, org_id, "asset:read", "查看媒体资产")
    if mime_type is not None and mime_type not in MIME_TYPES:
        raise BusinessError("mime_type 非法（image|video|audio|other）",
                            code=error_codes.INVALID_PARAM, http_status=400)
    query = (db.query(models.Asset)
             .filter(models.Asset.org_id == org_id,
                     models.Asset.status != models.ASSET_STATUS_DELETED))
    if q:
        query = query.filter(models.Asset.filename.ilike(f"%{q}%"))
    if work_id:
        query = query.filter(models.Asset.work_id == work_id)
    if mime_type and mime_type in MIME_PREFIX:
        query = query.filter(models.Asset.mime.like(f"{MIME_PREFIX[mime_type]}%"))
    elif mime_type == "other":
        query = query.filter(~models.Asset.mime.like("image/%"),
                             ~models.Asset.mime.like("video/%"),
                             ~models.Asset.mime.like("audio/%"))
    assets = query.order_by(models.Asset.created_at.desc()).limit(limit).all()
    titles = _work_titles(db, org_id)
    counts = _ref_counts(db, org_id, [a.id for a in assets])
    cards = [
        AssetCard(
            id=a.id, filename=a.filename or "", mime=a.mime or "",
            size=a.size or 0,
            created_at=a.created_at.isoformat() if a.created_at else None,
            work_id=a.work_id, work_title=titles.get(a.work_id, ""),
            ref_count=counts.get(a.id, 0),
        ).model_dump()
        for a in assets
    ]
    return success({"items": cards, "total": len(cards)})


# ---------- 素材详情（含引用关系） ----------

@router.get("/assets/{asset_id}", response_model=dict)
def asset_detail(
    asset_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """素材详情：来源项目 + 引用项目列表 + 引用计数（需 asset:read）。"""
    _require_lib_perm(db, user, org_id, "asset:read", "查看媒体资产")
    a = _alive_asset(db, asset_id, org_id)
    titles = _work_titles(db, org_id)
    refs = (db.query(models.AssetRef)
            .filter(models.AssetRef.org_id == org_id,
                    models.AssetRef.asset_id == asset_id)
            .order_by(models.AssetRef.created_at.desc()).all())
    return success({
        "id": a.id, "filename": a.filename or "", "mime": a.mime or "",
        "size": a.size or 0,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "source_work_id": a.work_id,
        "source_work_title": titles.get(a.work_id, ""),
        "ref_count": len(refs),
        "refs": [_ref_out(db, r, titles).model_dump() for r in refs],
    })


# ---------- 创建引用（引用到当前项目） ----------

@router.post("/refs", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_ref(
    req: RefCreateReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把资产引用到目标项目：只记引用关系，不复制文件（需 asset:upload）。

    - 资产与目标项目必须同属该组织；
    - 不能引用回自己的来源项目（无意义）；
    - 同一项目重复引用同一素材 → 409/ASSET_REF_CONFLICT。
    """
    _require_lib_perm(db, user, req.org_id, "asset:upload", "引用媒体素材")
    asset = _alive_asset(db, req.asset_id, req.org_id)
    dest = _alive_work(db, req.dest_work_id, req.org_id)
    if dest.id == asset.work_id:
        raise BusinessError("不能引用素材到它自己的来源项目",
                            code=error_codes.INVALID_PARAM, http_status=400)
    exists = (db.query(models.AssetRef)
              .filter(models.AssetRef.org_id == req.org_id,
                      models.AssetRef.asset_id == req.asset_id,
                      models.AssetRef.dest_work_id == dest.id)
              .first())
    if exists is not None:
        raise BusinessError("该项目已引用过这个素材",
                            code=error_codes.ASSET_REF_CONFLICT,
                            http_status=409)
    ref = models.AssetRef(
        org_id=req.org_id, asset_id=asset.id,
        source_work_id=asset.work_id, dest_work_id=dest.id,
        created_by=user.id,
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    titles = _work_titles(db, req.org_id)
    return success(_ref_out(db, ref, titles).model_dump())


# ---------- 解除引用（≠ 删除实体） ----------

@router.delete("/refs/{ref_id}", response_model=dict)
def remove_ref(
    ref_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """解除引用关系：只删引用记录，资产实体与文件不受影响（需 asset:delete）。"""
    _require_lib_perm(db, user, org_id, "asset:delete", "解除素材引用")
    ref = (db.query(models.AssetRef)
           .filter(models.AssetRef.id == ref_id,
                   models.AssetRef.org_id == org_id)
           .first())
    if ref is None:
        raise BusinessError("素材引用不存在",
                            code=error_codes.ASSET_REF_NOT_FOUND,
                            http_status=404)
    db.delete(ref)
    db.commit()
    return success({"id": ref_id, "removed": True})


# ---------- 目标项目视角：项目 B 引用了哪些外来素材 ----------

@router.get("/works/{work_id}/refs", response_model=dict)
def refs_by_work(
    work_id: str,
    org_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """某项目引用的全部外来素材（需 asset:read）。"""
    _require_lib_perm(db, user, org_id, "asset:read", "查看媒体资产")
    _alive_work(db, work_id, org_id)
    refs = (db.query(models.AssetRef)
            .filter(models.AssetRef.org_id == org_id,
                    models.AssetRef.dest_work_id == work_id)
            .order_by(models.AssetRef.created_at.desc()).all())
    titles = _work_titles(db, org_id)
    return success({"work_id": work_id,
                    "items": [_ref_out(db, r, titles).model_dump()
                              for r in refs]})
