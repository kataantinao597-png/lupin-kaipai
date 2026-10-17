"""作品 API（047/048/050）：租户隔离新体系。

- POST   /api/v1/works                 创建作品（必须指定 org_id，需 work:create）
- GET    /api/v1/works?org_id=...     列出某组织的作品（048：查询强制带 org 上下文）
- GET    /api/v1/works/{work_id}       作品详情（047 对象级权限）
- PATCH  /api/v1/works/{work_id}       更新作品（047）
- DELETE /api/v1/works/{work_id}       删除作品（050：二次验证）
- POST   /api/v1/works/{work_id}/export-all  导出全部数据（050：二次验证）

现有 localStorage 作品暂不迁移（Phase 3），此为新体系起点。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import (
    get_membership, has_permission, require_work_access, assert_can,
)
from .schemas import WorkCreate, WorkUpdate, WorkOut

router = APIRouter(prefix="/works", tags=["v1-works"])


def _work_out(work: models.Work) -> WorkOut:
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return WorkOut(
        id=work.id, org_id=work.org_id, owner_id=work.owner_id,
        title=work.title, data=data,
        created_at=work.created_at, updated_at=work.updated_at,
    )


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
    """048：租户隔离——作品列表查询强制带 org 上下文，无 org_id 参数直接 400。"""
    if not org_id:
        raise HTTPException(status_code=400, detail="必须指定 org_id")
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "work:read"):
        raise HTTPException(status_code=404, detail="组织不存在")
    works = (
        db.query(models.Work)
        .filter(models.Work.org_id == org_id)  # 048：强制 org 过滤
        .order_by(models.Work.updated_at.desc())
        .all()
    )
    return [_work_out(w) for w in works]


@router.get("/{work_id}", response_model=WorkOut)
def get_work(
    access: tuple = Depends(require_work_access("work:read")),
):
    work, _ms = access
    return _work_out(work)


@router.patch("/{work_id}", response_model=WorkOut)
def update_work(
    work_id: str,
    req: WorkUpdate,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
):
    work, _ms = access
    # 重新从当前会话加载以获得可写对象（Depends 内会话已关闭）
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if req.title is not None:
        work.title = req.title.strip()
    if req.data is not None:
        work.data_json = json.dumps(req.data, ensure_ascii=False)
    db.commit()
    db.refresh(work)
    return _work_out(work)


@router.delete("/{work_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work(
    work_id: str,
    access: tuple = Depends(require_work_access("work:delete")),
    db: Session = Depends(get_db),
):
    """050：删除作品是重要操作——路由 Depends 之外，handler 内二次验证权限。"""
    work, ms = access
    assert_can(ms.role, "work:delete", "删除作品")  # 050 二次验证
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    db.delete(work)
    db.commit()
    return None


@router.post("/{work_id}/export-all")
def export_all(
    work_id: str,
    access: tuple = Depends(require_work_access("work:export")),
    db: Session = Depends(get_db),
):
    """050：导出全部数据是重要操作——二次验证权限，不只靠前端按钮隐藏。"""
    work, ms = access
    assert_can(ms.role, "work:export", "导出全部数据")  # 050 二次验证
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    return {
        "work": _work_out(work).model_dump(),
        "exported_by_role": ms.role,
    }
