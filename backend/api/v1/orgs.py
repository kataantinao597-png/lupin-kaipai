"""组织管理 API（045/046/049/050）。

- POST   /api/v1/orgs                        创建组织（创建者自动成为 owner）
- GET    /api/v1/orgs                        我的组织列表
- GET    /api/v1/orgs/{org_id}               组织详情（需 org:read）
- DELETE /api/v1/orgs/{org_id}               删除组织（仅 owner，050 二次验证）
- POST   /api/v1/orgs/{org_id}/invite        生成邀请码，7 天有效（需 org:member:invite）
- POST   /api/v1/orgs/join                   凭邀请码加入组织
- GET    /api/v1/orgs/{org_id}/members       成员列表（需 org:read）
- PATCH  /api/v1/orgs/{org_id}/members/{uid} 修改成员角色（049：仅 owner/admin）
- DELETE /api/v1/orgs/{org_id}/members/{uid} 移除成员（049：仅 owner/admin）
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import (
    VALID_ROLES, ROLE_OWNER, has_permission, get_membership,
    require_org_permission, assert_can,
)
from .schemas import (
    OrgCreate, OrgOut, InviteCreate, InviteOut, JoinRequest,
    MemberOut, MemberRoleUpdate,
)

router = APIRouter(prefix="/orgs", tags=["v1-orgs"])

INVITE_TTL = timedelta(days=7)


def _aware(dt: datetime) -> datetime:
    """SQLite 读出的 DateTime(timezone=True) 会丢失 tzinfo，比较前归一化为 UTC。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _org_out(org: models.Organization, my_role: str) -> OrgOut:
    return OrgOut(
        id=org.id, name=org.name, created_by=org.created_by,
        created_at=org.created_at, my_role=my_role,
    )


@router.post("", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
def create_org(
    req: OrgCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """045：创建组织，创建者自动成为 owner。"""
    org = models.Organization(name=req.name.strip(), created_by=user.id)
    db.add(org)
    db.flush()  # 拿到 org.id
    db.add(models.Membership(user_id=user.id, org_id=org.id, role=ROLE_OWNER))
    db.commit()
    db.refresh(org)
    return _org_out(org, ROLE_OWNER)


@router.get("", response_model=list[OrgOut])
def list_my_orgs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """045：我的组织列表（含我在每个组织的角色）。"""
    memberships = (
        db.query(models.Membership)
        .filter(models.Membership.user_id == user.id)
        .all()
    )
    if not memberships:
        return []
    org_ids = [m.org_id for m in memberships]
    orgs = db.query(models.Organization).filter(
        models.Organization.id.in_(org_ids)).all()
    org_map = {o.id: o for o in orgs}
    return [_org_out(org_map[m.org_id], m.role) for m in memberships
            if m.org_id in org_map]


@router.get("/{org_id}", response_model=OrgOut)
def get_org(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    org = db.query(models.Organization).filter(
        models.Organization.id == org_id).first()
    return _org_out(org, ms.role)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_org(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:delete")),
    db: Session = Depends(get_db),
):
    """050：删除组织是重要操作，handler 内二次验证权限。"""
    assert_can(ms.role, "org:delete", "删除组织")
    # 级联清理：成员、邀请、作品（新体系作品）
    db.query(models.Membership).filter(
        models.Membership.org_id == org_id).delete()
    db.query(models.Invitation).filter(
        models.Invitation.org_id == org_id).delete()
    db.query(models.Work).filter(models.Work.org_id == org_id).delete()
    db.query(models.Organization).filter(
        models.Organization.id == org_id).delete()
    db.commit()
    return None


@router.post("/{org_id}/invite", response_model=InviteOut)
def create_invite(
    org_id: str,
    req: InviteCreate,
    user: User = Depends(get_current_user),
    ms: models.Membership = Depends(require_org_permission("org:member:invite")),
    db: Session = Depends(get_db),
):
    """045：生成邀请码，7 天有效。"""
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色: {req.role}")
    if req.role == ROLE_OWNER:
        raise HTTPException(status_code=400, detail="不能通过邀请码直接授予 owner")
    now = datetime.now(timezone.utc)
    inv = models.Invitation(
        org_id=org_id,
        code=secrets.token_urlsafe(12),
        role=req.role,
        created_by=user.id,
        expires_at=now + INVITE_TTL,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return InviteOut(code=inv.code, org_id=org_id, role=inv.role,
                     expires_at=inv.expires_at)


@router.post("/join", response_model=OrgOut)
def join_org(
    req: JoinRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """凭邀请码加入组织。"""
    inv = db.query(models.Invitation).filter(
        models.Invitation.code == req.code.strip()).first()
    now = datetime.now(timezone.utc)
    if inv is None or _aware(inv.expires_at) < now:
        raise HTTPException(status_code=400, detail="邀请码无效或已过期")
    existing = get_membership(db, user.id, inv.org_id)
    if existing is not None:
        raise HTTPException(status_code=400, detail="已是该组织成员")
    db.add(models.Membership(user_id=user.id, org_id=inv.org_id, role=inv.role))
    inv.used_by = user.id
    db.commit()
    org = db.query(models.Organization).filter(
        models.Organization.id == inv.org_id).first()
    return _org_out(org, inv.role)


@router.get("/{org_id}/members", response_model=list[MemberOut])
def list_members(
    org_id: str,
    ms: models.Membership = Depends(require_org_permission("org:read")),
    db: Session = Depends(get_db),
):
    members = (
        db.query(models.Membership)
        .filter(models.Membership.org_id == org_id)
        .order_by(models.Membership.joined_at)
        .all()
    )
    return [MemberOut(user_id=m.user_id, role=m.role, joined_at=m.joined_at)
            for m in members]


@router.patch("/{org_id}/members/{target_user_id}", response_model=MemberOut)
def set_member_role(
    org_id: str,
    target_user_id: str,
    req: MemberRoleUpdate,
    ms: models.Membership = Depends(require_org_permission("org:member:set_role")),
    db: Session = Depends(get_db),
):
    """049：修改成员角色仅 owner/admin 可执行（rbac.has_permission 硬限制）。"""
    assert_can(ms.role, "org:member:set_role", "修改成员角色")
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色: {req.role}")
    target = get_membership(db, target_user_id, org_id)
    if target is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    # 组织至少保留一个 owner
    if target.role == ROLE_OWNER and req.role != ROLE_OWNER:
        owners = (
            db.query(models.Membership)
            .filter(models.Membership.org_id == org_id,
                    models.Membership.role == ROLE_OWNER)
            .count()
        )
        if owners <= 1:
            raise HTTPException(status_code=400, detail="组织至少保留一个 owner")
    target.role = req.role
    db.commit()
    return MemberOut(user_id=target.user_id, role=target.role,
                     joined_at=target.joined_at)


@router.delete("/{org_id}/members/{target_user_id}",
              status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    org_id: str,
    target_user_id: str,
    user: User = Depends(get_current_user),
    ms: models.Membership = Depends(require_org_permission("org:member:remove")),
    db: Session = Depends(get_db),
):
    """049：移除成员仅 owner/admin 可执行。"""
    assert_can(ms.role, "org:member:remove", "移除成员")
    target = get_membership(db, target_user_id, org_id)
    if target is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if target.role == ROLE_OWNER:
        owners = (
            db.query(models.Membership)
            .filter(models.Membership.org_id == org_id,
                    models.Membership.role == ROLE_OWNER)
            .count()
        )
        if owners <= 1:
            raise HTTPException(status_code=400, detail="不能移除组织最后一个 owner")
    if target_user_id == user.id:
        raise HTTPException(status_code=400, detail="不能移除自己，请转让 owner 后退出")
    db.delete(target)
    db.commit()
    return None
