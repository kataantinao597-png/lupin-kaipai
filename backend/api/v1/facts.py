"""事实库 API（S6.1：状态机 + 变更审计 + 内容版本）。

- POST   /api/v1/facts                        创建事实（初始 draft，需 fact:create）
- GET    /api/v1/facts?org_id=&status=         事实列表（需 fact:read）
- GET    /api/v1/facts/{id}?org_id=            事实详情（需 fact:read）
- POST   /api/v1/facts/{id}/transition        状态流转（提交需 fact:update；
                                              确认/驳回/撤销需 fact:approve）
- GET    /api/v1/facts/{id}/history?org_id=    变更审计（需 fact:read）
- PATCH  /api/v1/facts/{id}/content           更新内容（版本快照，需 fact:update）
- GET    /api/v1/facts/{id}/versions?org_id=   内容版本快照（需 fact:read）
- POST   /api/v1/facts/expire-sweep           过期扫库（需 fact:approve）

S6.1：所有状态变更经状态机校验并写审计；确认/驳回/撤销必须填原因。
"""
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import get_membership, has_permission, assert_can
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...core.response import ApiResponse, success
from ...repositories.base import EntityNotFound, OptimisticLockError
from ...repositories.fact_repository import FactRepository, FactTransitionError
from ...facts import state_machine as sm
from .schemas import (
    FactCreate, FactOut, FactTransitionReq, FactHistoryOut,
    FactContentUpdate, FactVersionOut,
)

router = APIRouter(prefix="/facts", tags=["v1-facts"])

# 目标状态 -> 所需权限（S6.1：确认类动作为审批权限）
_TRANSITION_PERMISSIONS = {
    sm.PENDING_REVIEW: "fact:update",   # 提交审核 / 重新提交
    sm.DRAFT: "fact:update",            # 驳回后退回草稿修改
    sm.CONFIRMED: "fact:approve",       # 确认
    sm.REJECTED: "fact:approve",        # 驳回
    sm.REVOKED: "fact:approve",         # 撤销
    sm.EXPIRED: "fact:approve",         # 人工标记过期
}


def _fact_out(f: models.Fact) -> FactOut:
    return FactOut(
        id=f.id, org_id=f.org_id, product_id=f.product_id or "",
        content=f.content or "", category=f.category or "",
        source_type=f.source_type or "", source_ref=f.source_ref or "",
        status=sm.normalize_status(f.status),
        allow_use=bool(f.allow_use),
        verified_by=f.verified_by, verified_at=f.verified_at,
        expires_at=f.expires_at, version=f.version or 1,
        created_at=f.created_at, updated_at=f.updated_at,
    )


def _require_fact_perm(db: Session, user: User, org_id: str,
                       action: str, what: str):
    """取成员身份并校验权限；无权一律 404（防枚举，与 works.py 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)  # 050 二次验证
    return ms


@router.post("", response_model=ApiResponse[FactOut],
             status_code=status.HTTP_201_CREATED)
def create_fact(
    req: FactCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建事实（初始状态 draft）。需要 fact:create 权限。"""
    _require_fact_perm(db, user, req.org_id, "fact:create", "创建事实")
    repo = FactRepository(db)
    fact = repo.create(
        org_id=req.org_id, content=req.content,
        product_id=req.product_id, category=req.category,
        source_type=req.source_type, source_ref=req.source_ref,
        expires_at=req.expires_at,
    )
    return success(_fact_out(fact))


@router.get("", response_model=ApiResponse[list[FactOut]])
def list_facts(
    org_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """事实列表（可按状态过滤）。需要 fact:read 权限。"""
    _require_fact_perm(db, user, org_id, "fact:read", "查看事实库")
    repo = FactRepository(db)
    try:
        facts = repo.list_by_org(org_id, status=status_filter)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success([_fact_out(f) for f in facts])


@router.get("/{fact_id}", response_model=ApiResponse[FactOut])
def get_fact(
    fact_id: str,
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """事实详情。需要 fact:read 权限。"""
    _require_fact_perm(db, user, org_id, "fact:read", "查看事实")
    fact = FactRepository(db).get(fact_id, org_id)
    if fact is None:
        raise BusinessError("事实不存在",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    return success(_fact_out(fact))


@router.post("/{fact_id}/transition", response_model=ApiResponse[FactOut])
def transition_fact(
    fact_id: str,
    req: FactTransitionReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S6.1：事实状态流转。

    - 提交审核（draft→pending_review）：需 fact:update
    - 确认/驳回/撤销（→confirmed/rejected/revoked）：需 fact:approve，
      且必须填写 reason；确认还要求 allow_use=true
    - 非法流转 → 400；版本冲突 → 409
    """
    target = sm.normalize_status(req.target_status)
    need = _TRANSITION_PERMISSIONS.get(target)
    if need is None:
        raise BusinessError(f"未知目标状态: {req.target_status}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    _require_fact_perm(db, user, req.org_id, need, f"事实流转到 {target}")
    repo = FactRepository(db)
    try:
        fact = repo.transition(
            fact_id, req.org_id, target,
            operator_id=user.id,
            reason=req.reason, evidence_ref=req.evidence_ref,
            expected_version=req.expected_version,
            allow_use=req.allow_use,
        )
    except EntityNotFound:
        raise BusinessError("事实不存在",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    except OptimisticLockError as e:
        raise BusinessError(
            f"事实已被他人修改（期望版本 {e.expected}，当前 {e.actual}），"
            "请刷新后重试",
            code=error_codes.VERSION_CONFLICT, http_status=409)
    except (FactTransitionError,) as e:
        raise BusinessError(str(e), code=error_codes.FACT_UNVERIFIED,
                            http_status=400)
    return success(_fact_out(fact))


@router.get("/{fact_id}/history",
            response_model=ApiResponse[list[FactHistoryOut]])
def fact_history(
    fact_id: str,
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S6.1：事实状态变更审计记录（时间倒序）。需要 fact:read 权限。"""
    _require_fact_perm(db, user, org_id, "fact:read", "查看事实审计")
    repo = FactRepository(db)
    if repo.get(fact_id, org_id) is None:
        raise BusinessError("事实不存在",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    rows = repo.list_history(fact_id, org_id)
    return success([FactHistoryOut(
        id=r.id, fact_id=r.fact_id,
        from_status=r.from_status, to_status=r.to_status,
        operator_id=r.operator_id, reason=r.reason or "",
        evidence_ref=r.evidence_ref or "", created_at=r.created_at,
    ) for r in rows])


@router.patch("/{fact_id}/content", response_model=ApiResponse[FactOut])
def update_fact_content(
    fact_id: str,
    req: FactContentUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S6.1：更新事实内容。变更前自动存快照到 fact_versions，version+1。
    需要 fact:update 权限；expected_version 必填（乐观锁）。"""
    _require_fact_perm(db, user, req.org_id, "fact:update", "修改事实内容")
    repo = FactRepository(db)
    try:
        fact = repo.update_content(
            fact_id, req.org_id, user.id, req.expected_version,
            content=req.content, category=req.category,
            source_type=req.source_type, source_ref=req.source_ref,
        )
    except EntityNotFound:
        raise BusinessError("事实不存在",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    except OptimisticLockError as e:
        raise BusinessError(
            f"事实已被他人修改（期望版本 {e.expected}，当前 {e.actual}），"
            "请刷新后重试",
            code=error_codes.VERSION_CONFLICT, http_status=409)
    return success(_fact_out(fact))


@router.get("/{fact_id}/versions",
            response_model=ApiResponse[list[FactVersionOut]])
def fact_versions(
    fact_id: str,
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S6.1：事实内容版本快照（version_no 倒序）。需要 fact:read 权限。"""
    _require_fact_perm(db, user, org_id, "fact:read", "查看事实版本")
    repo = FactRepository(db)
    try:
        rows = repo.list_versions(fact_id, org_id)
    except EntityNotFound:
        raise BusinessError("事实不存在",
                            code=error_codes.FACT_NOT_FOUND, http_status=404)
    return success([FactVersionOut(
        id=r.id, fact_id=r.fact_id, version_no=r.version_no,
        content=r.content or "", category=r.category or "",
        source_type=r.source_type or "", source_ref=r.source_ref or "",
        status=r.status or "", allow_use=bool(r.allow_use),
        created_by=r.created_by or "", created_at=r.created_at,
    ) for r in rows])


@router.post("/expire-sweep", response_model=ApiResponse[dict])
def expire_sweep(
    org_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S6.1：将已过有效期的 confirmed 事实自动转 expired。
    需要 fact:approve 权限。不传 org_id 则扫全库（仅限有任一组织
    fact:approve 权限的用户；生产建议用 scripts/expire_facts.py 挂 cron）。"""
    if org_id:
        _require_fact_perm(db, user, org_id, "fact:approve", "执行过期扫库")
        n = FactRepository(db).expire_due(org_id)
    else:
        # 全库扫：要求用户至少在一个组织有 fact:approve
        ms_list = (db.query(models.Membership)
                   .filter(models.Membership.user_id == user.id).all())
        if not any(has_permission(m.role, "fact:approve") for m in ms_list):
            raise AuthError("无权执行全库过期扫库", forbidden=True)
        n = FactRepository(db).expire_due(None)
    return success({"expired": n})
