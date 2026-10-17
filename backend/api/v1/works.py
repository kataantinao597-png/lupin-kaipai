"""作品 API（047/048/050/091/092/099）：租户隔离新体系。

- POST   /api/v1/works                       创建作品（必须指定 org_id，需 work:create）
- GET    /api/v1/works?org_id=...             列出某组织的作品（048：强制 org 上下文；092：排除回收站）
- GET    /api/v1/works/trash?org_id=...       092：回收站列表（需 work:delete 权限）
- GET    /api/v1/works/{work_id}             作品详情（047 对象级权限；092：回收站作品返回 404）
- PATCH  /api/v1/works/{work_id}             更新作品（047；091：data 变更前自动存快照）
- DELETE /api/v1/works/{work_id}             092：软删除进回收站；?hard=true 时 owner 且满 30 天可硬删除
- POST   /api/v1/works/{work_id}/restore     092：从回收站恢复
- GET    /api/v1/works/{work_id}/versions     091：历史版本列表
- POST   /api/v1/works/{work_id}/versions/{v}/restore  091：恢复到指定版本（先快照当前）
- GET    /api/v1/works/{work_id}/export-json 099：导出作品完整 JSON（含分镜、事实、版本历史）
- POST   /api/v1/works/{work_id}/export-all  导出全部数据（050：二次验证）

现有 localStorage 作品暂不迁移（Phase 3），此为新体系起点。

注意：FastAPI 按声明顺序匹配路由，/trash 与 /{id}/xxx 等固定路径
必须声明在 /{work_id} 之前，否则会被 {work_id} 吞掉。
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import (
    get_membership, has_permission, require_work_access, assert_can,
    ROLE_OWNER,
)
from .schemas import WorkCreate, WorkUpdate, WorkOut, WorkVersionOut, TrashWorkOut

router = APIRouter(prefix="/works", tags=["v1-works"])

# 091：单个作品最多保留快照数，超限删除最旧
MAX_VERSIONS_PER_WORK = 50


def _work_out(work: models.Work) -> WorkOut:
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return WorkOut(
        id=work.id, org_id=work.org_id, owner_id=work.owner_id,
        title=work.title, data=data,
        status=work.status or "draft", version=work.version or 1,
        created_at=work.created_at, updated_at=work.updated_at,
    )


def _version_out(v: models.WorkVersion) -> WorkVersionOut:
    return WorkVersionOut(
        id=v.id, work_id=v.work_id, version_no=v.version_no,
        title=v.title, created_by=v.created_by, created_at=v.created_at,
    )


def _snapshot_work(db: Session, work: models.Work, user_id: str) -> models.WorkVersion:
    """091：修改前存快照。返回新版本记录。"""
    last = (db.query(models.WorkVersion)
            .filter(models.WorkVersion.work_id == work.id)
            .order_by(models.WorkVersion.version_no.desc())
            .first())
    next_no = (last.version_no + 1) if last else 1
    snap = models.WorkVersion(
        work_id=work.id,
        version_no=next_no,
        snapshot_json=work.data_json or "{}",
        title=work.title or "",
        created_by=user_id,
    )
    db.add(snap)
    db.flush()
    # 超限：删除最旧的快照
    excess = (db.query(models.WorkVersion)
              .filter(models.WorkVersion.work_id == work.id)
              .order_by(models.WorkVersion.version_no.asc())
              .offset(MAX_VERSIONS_PER_WORK)
              .all())
    for old in excess:
        db.delete(old)
    return snap


def _get_active_work(db: Session, work_id: str) -> models.Work:
    """取非回收站作品；回收站中的返回 404（092：正常业务流不可见已删除作品）。"""
    work = (db.query(models.Work)
            .filter(models.Work.id == work_id,
                    models.Work.deleted_at.is_(None))
            .first())
    if work is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    return work


@router.post("", response_model=WorkOut, status_code=status.HTTP_201_CREATED)
def create_work(
    req: WorkCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """048：作品创建时必须指定 org_id；调用者在该组织内需有 work:create。"""
    org = db.query(models.Organization).filter(
        models.Organization.id == req.org_id).first()
    if org is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    ms = get_membership(db, user.id, req.org_id)
    if ms is None or not has_permission(ms.role, "work:create"):
        # 防枚举：与"组织不存在"返回一致
        raise HTTPException(status_code=404, detail="组织不存在")
    work = models.Work(
        org_id=req.org_id,
        owner_id=user.id,
        title=req.title.strip(),
        data_json=json.dumps(req.data or {}, ensure_ascii=False),
    )
    db.add(work)
    db.commit()
    db.refresh(work)
    return _work_out(work)


@router.get("", response_model=list[WorkOut])
def list_works(
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """048：租户隔离——作品列表查询强制带 org 上下文，无 org_id 参数直接 400。
    092：排除回收站（deleted_at 非空的不返回）。"""
    if not org_id:
        raise HTTPException(status_code=400, detail="必须指定 org_id")
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "work:read"):
        raise HTTPException(status_code=404, detail="组织不存在")
    works = (
        db.query(models.Work)
        .filter(models.Work.org_id == org_id,          # 048：强制 org 过滤
                models.Work.deleted_at.is_(None))      # 092：排除回收站
        .order_by(models.Work.updated_at.desc())
        .all()
    )
    return [_work_out(w) for w in works]


@router.get("/trash", response_model=list[TrashWorkOut])
def list_trash(
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """092：回收站列表。需要 work:delete 权限（能删才能看回收站）。"""
    if not org_id:
        raise HTTPException(status_code=400, detail="必须指定 org_id")
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "work:delete"):
        raise HTTPException(status_code=404, detail="组织不存在")
    works = (
        db.query(models.Work)
        .filter(models.Work.org_id == org_id,
                models.Work.deleted_at.isnot(None))
        .order_by(models.Work.deleted_at.desc())
        .all()
    )
    out = []
    for w in works:
        wo = _work_out(w)
        out.append(TrashWorkOut(**wo.model_dump(), deleted_at=w.deleted_at))
    return out


@router.get("/{work_id}", response_model=WorkOut)
def get_work(
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    work, _ms = access
    # 092：回收站作品对正常详情接口不可见
    work = _get_active_work(db, work.id)
    return _work_out(work)


@router.patch("/{work_id}", response_model=WorkOut)
def update_work(
    work_id: str,
    req: WorkUpdate,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    work, _ms = access
    # 重新从当前会话加载以获得可写对象（Depends 内会话已关闭）
    work = _get_active_work(db, work_id)
    # 088：带 version 时先做乐观锁检查（在快照之前，避免冲突产生脏快照）；
    # 行锁保证检查-更新原子性（PostgreSQL 生效，SQLite 写串行化）。
    if req.version is not None:
        work = (db.query(models.Work)
                .filter(models.Work.id == work_id,
                        models.Work.deleted_at.is_(None))
                .with_for_update()
                .first())
        if work is None:
            raise HTTPException(status_code=404, detail="作品不存在")
        if work.version != req.version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"作品已被他人修改（当前版本 {work.version}），请刷新后重试",
            )
    # 091：data 实质变更前先存快照（仅改标题不触发快照）
    if req.data is not None:
        _snapshot_work(db, work, user.id)
        work.data_json = json.dumps(req.data, ensure_ascii=False)
    if req.title is not None:
        work.title = req.title.strip()
    if req.status is not None:
        if req.status not in ("draft", "active", "archived"):
            raise HTTPException(status_code=422, detail="非法 status")
        work.status = req.status
    if req.version is not None:
        work.version = work.version + 1
    db.commit()
    db.refresh(work)
    return _work_out(work)


@router.delete("/{work_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work(
    work_id: str,
    hard: bool = Query(default=False, description="硬删除：仅 owner 且回收站满 30 天"),
    access: tuple = Depends(require_work_access("work:delete")),
    db: Session = Depends(get_db),
):
    """092：默认软删除（进回收站，可恢复）。
    050：删除作品是重要操作——路由 Depends 之外，handler 内二次验证权限。
    硬删除：只允许 owner，且作品必须在回收站满 30 天。
    """
    work, ms = access
    assert_can(ms.role, "work:delete", "删除作品")  # 050 二次验证
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None:  # 已被硬删除的极端竞态
        raise HTTPException(status_code=404, detail="作品不存在")

    if hard:
        if ms.role != ROLE_OWNER:
            raise HTTPException(status_code=403, detail="硬删除只允许组织 owner 执行")
        if work.deleted_at is None:
            raise HTTPException(status_code=400,
                                detail="作品不在回收站，不能直接硬删除（请先软删除）")
        deleted_at = work.deleted_at
        if deleted_at.tzinfo is None:
            # SQLite 存入的 datetime 可能丢失 tzinfo，统一按 UTC 处理
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - deleted_at).days < 30:
            raise HTTPException(status_code=400,
                                detail="回收站未满 30 天，不能硬删除（到期由清理任务自动处理）")
        db.query(models.WorkVersion).filter(
            models.WorkVersion.work_id == work.id).delete()
        db.delete(work)
        db.commit()
        return None

    if work.deleted_at is not None:
        # 已在回收站：幂等返回
        return None
    work.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None


@router.post("/{work_id}/restore", response_model=WorkOut)
def restore_work(
    work_id: str,
    access: tuple = Depends(require_work_access("work:delete")),
    db: Session = Depends(get_db),
):
    """092：从回收站恢复作品。需要 work:delete 权限。"""
    work, ms = access
    assert_can(ms.role, "work:delete", "恢复作品")
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None or work.deleted_at is None:
        raise HTTPException(status_code=404, detail="回收站中没有该作品")
    work.deleted_at = None
    db.commit()
    db.refresh(work)
    return _work_out(work)


@router.get("/{work_id}/versions", response_model=list[WorkVersionOut])
def list_versions(
    work_id: str,
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """091：历史版本列表（新到旧）。回收站作品不可查。"""
    work, _ms = access
    _get_active_work(db, work.id)
    versions = (db.query(models.WorkVersion)
                .filter(models.WorkVersion.work_id == work_id)
                .order_by(models.WorkVersion.version_no.desc())
                .all())
    return [_version_out(v) for v in versions]


@router.post("/{work_id}/versions/{version_no}/restore", response_model=WorkOut)
def restore_version(
    work_id: str,
    version_no: int,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """091：恢复到指定版本。恢复前先把当前状态存为快照（恢复操作本身可撤销）。"""
    work, _ms = access
    work = _get_active_work(db, work_id)
    ver = (db.query(models.WorkVersion)
           .filter(models.WorkVersion.work_id == work_id,
                   models.WorkVersion.version_no == version_no)
           .first())
    if ver is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    _snapshot_work(db, work, user.id)  # 当前状态先存档
    work.data_json = ver.snapshot_json
    work.title = ver.title
    db.commit()
    db.refresh(work)
    return _work_out(work)


@router.get("/{work_id}/export-json")
def export_json(
    work_id: str,
    access: tuple = Depends(require_work_access("work:export")),
    db: Session = Depends(get_db),
):
    """099：导出作品完整 JSON（含分镜、事实、版本历史），供备份/迁移。
    050：导出是重要操作——二次验证权限。"""
    work, ms = access
    assert_can(ms.role, "work:export", "导出作品数据")
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None or work.deleted_at is not None:
        raise HTTPException(status_code=404, detail="作品不存在")
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    versions = (db.query(models.WorkVersion)
                .filter(models.WorkVersion.work_id == work_id)
                .order_by(models.WorkVersion.version_no.asc())
                .all())
    return {
        "format": "lupin-work-export",
        "format_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "work": {
            "id": work.id,
            "org_id": work.org_id,
            "owner_id": work.owner_id,
            "title": work.title,
            "created_at": work.created_at.isoformat() if work.created_at else None,
            "updated_at": work.updated_at.isoformat() if work.updated_at else None,
            "data": data,   # 含 product/facts/storyboard/shoot 等全部业务数据
        },
        "versions": [
            {
                "version_no": v.version_no,
                "title": v.title,
                "created_by": v.created_by,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "snapshot": json.loads(v.snapshot_json or "{}"),
            }
            for v in versions
        ],
    }


@router.post("/{work_id}/export-all")
def export_all(
    work_id: str,
    access: tuple = Depends(require_work_access("work:export")),
    db: Session = Depends(get_db),
):
    """050：导出全部数据是重要操作——二次验证权限，不只靠前端按钮隐藏。"""
    work, ms = access
    assert_can(ms.role, "work:export", "导出全部数据")  # 050 二次验证
    work = _get_active_work(db, work.id)
    return {
        "work": _work_out(work).model_dump(),
        "exported_by_role": ms.role,
    }
