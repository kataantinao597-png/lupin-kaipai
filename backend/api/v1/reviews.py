"""S6：审核闭环 API（v1 新体系）。

027：全部接口使用标准响应信封，错误用 error_codes 业务码。

works 作用域（prefix /works）：
- POST   /works/{work_id}/reviews                    创建审核（绑定内容哈希+版本）
- GET    /works/{work_id}/reviews                    审核历史列表
- GET    /works/{work_id}/reviews/latest             最新审核（自动校验新鲜度）
- POST   /works/{work_id}/waivers                    创建交付豁免
- GET    /works/{work_id}/waivers                    豁免列表
- POST   /works/{work_id}/delivery-check             配置化交付检查（含豁免）

reviews 作用域（prefix /reviews）：
- POST   /reviews/{review_id}/transition             审核状态流转
- POST   /reviews/{review_id}/findings/import        从分镜 risk_flags 导入风险发现
- GET    /reviews/{review_id}/findings               风险发现列表

- PATCH  /risk-findings/{finding_id}                 处置风险发现
- DELETE /waivers/{waiver_id}                        撤销豁免
- GET    /delivery/gates/{gate_type}                 门禁配置查询
- PATCH  /delivery/gates/{gate_type}                 门禁配置调整（组织管理员）

注意：FastAPI 按声明顺序匹配路由，固定路径必须声明在 /{work_id} 之前。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...core.response import ApiResponse, success
from ...rbac import (
    ROLE_ADMIN,
    ROLE_OWNER,
    get_membership,
    has_permission,
    require_work_access,
)
from ...repositories import (
    EntityNotFound,
    IllegalFindingTransition,
    IllegalReviewTransition,
    OptimisticLockError,
    ReviewRepository,
    RiskFindingRepository,
)
from ...services import review_service
from ...services.delivery_gate_service import (
    create_waiver,
    get_active_waivers,
    get_gate_configs,
    list_waivers,
    revoke_waiver,
    upsert_gate_config,
)
from ...services.delivery_service import check_delivery

router = APIRouter(prefix="/works", tags=["v1-reviews"])
review_router = APIRouter(prefix="/reviews", tags=["v1-reviews"])
finding_router = APIRouter(prefix="/risk-findings", tags=["v1-reviews"])
waiver_router = APIRouter(prefix="/waivers", tags=["v1-reviews"])
delivery_router = APIRouter(tags=["v1-delivery"])


# ---------------------------------------------------------------- Schemas

class ReviewCreate(BaseModel):
    model: Optional[str] = None  # 本次审核实际使用的模型（默认取当前配置）


class ReviewTransition(BaseModel):
    to_status: str
    expected_version: int
    reviewer_id: Optional[str] = None
    decisions: Optional[dict] = None
    comment: str = ""


class FindingResolve(BaseModel):
    to_status: Literal["fixed", "waived"]


class WaiverCreate(BaseModel):
    gate_type: Literal["plan", "production", "final"]
    check_id: str
    reason: str = Field(min_length=1)
    expires_at: Optional[datetime] = None


class GateConfigUpdate(BaseModel):
    check_id: str
    is_blocking: Optional[bool] = None
    is_waivable: Optional[bool] = None


class V1DeliveryCheckRequest(BaseModel):
    check_type: Literal["plan", "final"] = "plan"
    storyboard: Optional[dict] = None
    product: Optional[dict] = None
    facts: List[dict] = Field(default_factory=list)
    feasibility: Optional[dict] = None
    shoot_state: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- 内部工具

def _review_out(r: models.Review, fresh: Optional[bool] = None,
                current_hash: Optional[str] = None) -> dict:
    d = {
        "id": r.id, "work_id": r.work_id, "org_id": r.org_id,
        "storyboard_id": r.storyboard_id,
        "content_hash": r.content_hash,
        "rule_version": r.rule_version or "",
        "model_version": r.model_version or "",
        "prompt_version": r.prompt_version or "",
        "status": r.status,
        "decisions": r.decisions_json or {},
        "reviewer_id": r.reviewer_id,
        "reviewed_at": r.reviewed_at,
        "comment": r.comment or "",
        "version": r.version,
        "created_at": r.created_at, "updated_at": r.updated_at,
    }
    if fresh is not None:
        d["fresh"] = fresh
        d["current_content_hash"] = current_hash or ""
    return d


def _finding_out(f: models.RiskFinding) -> dict:
    return {
        "id": f.id, "review_id": f.review_id, "work_id": f.work_id,
        "shot_id": f.shot_id, "shot_no": f.shot_no,
        "text_fragment": f.text_fragment,
        "severity": f.severity, "status": f.status,
        "source": f.source, "reason": f.reason,
        "resolved_by": f.resolved_by, "resolved_at": f.resolved_at,
        "created_at": f.created_at, "updated_at": f.updated_at,
    }


def _check_member(db: Session, user: User, org_id: str,
                  action: str) -> models.Membership:
    """对象级权限：非成员/无权限一律 404（防枚举，与 require_work_access 一致）。"""
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("资源不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    return ms


def _get_review_work(db: Session, review_id: str) -> tuple:
    """取审核及其作品；不存在一律 404（防枚举）。"""
    repo = ReviewRepository(db)
    review = repo.get(review_id)
    if review is None:
        raise BusinessError("审核不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    work = db.query(models.Work).filter(
        models.Work.id == review.work_id).first()
    if work is None:
        raise BusinessError("审核不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    return review, work


def _severity_of(level: str) -> str:
    return {"高": "high", "中": "medium", "低": "low"}.get(
        (level or "").strip(), "medium")


# ---------------------------------------------------------------- 审核

@router.post("/{work_id}/reviews",
             response_model=ApiResponse[dict])
def create_review(work_id: str, body: ReviewCreate,
                  ctx=Depends(require_work_access("review:submit")),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """S6.3：创建审核——计算当前内容哈希并绑定规则/模型/Prompt 版本。"""
    work, ms = ctx
    content_hash, storyboard_id = review_service.compute_content_hash(
        db, work_id)
    versions = review_service.current_versions(body.model)
    repo = ReviewRepository(db)
    review = repo.create(
        org_id=work.org_id, work_id=work_id, content_hash=content_hash,
        storyboard_id=storyboard_id, created_by=user.id,
        rule_version=versions["rule_version"],
        model_version=versions["model_version"],
        prompt_version=versions["prompt_version"],
    )
    db.commit()
    return success(_review_out(review, fresh=True,
                               current_hash=content_hash))


@router.get("/{work_id}/reviews",
            response_model=ApiResponse[dict])
def list_reviews(work_id: str,
                 limit: int = Query(50, le=200),
                 ctx=Depends(require_work_access("work:read")),
                 db: Session = Depends(get_db)):
    """审核历史列表（含已过期的审计记录）。"""
    repo = ReviewRepository(db)
    items = repo.list_by_work(work_id, limit=limit)
    return success({"items": [_review_out(r) for r in items],
                    "total": len(items)})


@router.get("/{work_id}/reviews/latest",
            response_model=ApiResponse[dict])
def latest_review(work_id: str,
                  ctx=Depends(require_work_access("work:read")),
                  db: Session = Depends(get_db)):
    """S6.3：最新审核——自动校验内容哈希，不一致则置 expired 并提示重审。"""
    repo = ReviewRepository(db)
    review = repo.latest_for_work(work_id)
    if review is None:
        raise BusinessError("暂无审核记录",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    fresh, current_hash = review_service.validate_freshness(db, review)
    db.commit()
    db.refresh(review)
    data = _review_out(review, fresh=fresh, current_hash=current_hash)
    if not fresh:
        data["stale_reason"] = "内容已变更，该审核结论已自动过期，请重新审核"
    return success(data)


@review_router.post("/{review_id}/transition",
                    response_model=ApiResponse[dict])
def transition_review(review_id: str, body: ReviewTransition,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """审核状态流转（乐观锁 version 必填）。

    权限：提交审核类流转需 review:submit；批准/驳回/要求修改需 review:approve。
    """
    review, work = _get_review_work(db, review_id)
    need = ("review:approve" if body.to_status in (
        "approved", "rejected", "changes_requested") else "review:submit")
    _check_member(db, user, work.org_id, need)
    repo = ReviewRepository(db)
    try:
        review = repo.transition(
            review_id, body.to_status, body.expected_version,
            reviewer_id=body.reviewer_id or user.id,
            decisions=body.decisions, comment=body.comment)
    except OptimisticLockError:
        raise BusinessError("审核已被他人更新，请刷新后重试",
                            code=error_codes.VERSION_CONFLICT, http_status=409)
    except IllegalReviewTransition as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=422)
    db.commit()
    return success(_review_out(review))


# ---------------------------------------------------------------- 风险发现

@review_router.post("/{review_id}/findings/import",
                    response_model=ApiResponse[dict])
def import_findings(review_id: str,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """S6.4：把作品分镜数据中的 risk_flags 导入为风险发现（定位到镜头）。"""
    review, work = _get_review_work(db, review_id)
    _check_member(db, user, work.org_id, "review:submit")
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    sb = data.get("storyboard") or {}
    flags = sb.get("risk_flags") or []
    # shot_no → 稳定 ID 映射（取最新分镜）
    shot_map: dict[int, str] = {}
    try:
        latest_sb = db.query(models.Storyboard).filter(
            models.Storyboard.work_id == work.id).order_by(
            models.Storyboard.version.desc()).first()
        if latest_sb:
            for s in db.query(models.Shot).filter(
                    models.Shot.storyboard_id == latest_sb.id).all():
                shot_map[int(s.shot_no)] = s.shot_id_stable or s.id
    except Exception:  # noqa: BLE001 — 映射失败不阻断导入
        pass
    frepo = RiskFindingRepository(db)
    created = []
    for fl in flags:
        shot_no = fl.get("shot_number")
        try:
            shot_no_i = int(shot_no) if shot_no is not None else None
        except (TypeError, ValueError):
            shot_no_i = None
        f = frepo.create(
            review_id=review.id, org_id=work.org_id, work_id=work.id,
            severity=_severity_of(fl.get("level")),
            source=fl.get("source") or "风险审核",
            reason=fl.get("reason") or "",
            text_fragment=fl.get("text") or "",
            shot_id=shot_map.get(shot_no_i) if shot_no_i else None,
            shot_no=shot_no_i,
        )
        created.append(_finding_out(f))
    db.commit()
    return success({"items": created, "total": len(created)})


@review_router.get("/{review_id}/findings",
                   response_model=ApiResponse[dict])
def list_findings(review_id: str,
                  status: Optional[str] = Query(None),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """风险发现列表（?status=open/fixed/waived 过滤）。"""
    review, work = _get_review_work(db, review_id)
    _check_member(db, user, work.org_id, "work:read")
    frepo = RiskFindingRepository(db)
    items = frepo.list_by_review(review_id, status=status)
    open_count = frepo.count_open(review_id)
    return success({"items": [_finding_out(f) for f in items],
                    "total": len(items), "open_count": open_count})


@finding_router.patch("/{finding_id}",
                      response_model=ApiResponse[dict])
def resolve_finding(finding_id: str, body: FindingResolve,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """S6.4：处置风险发现——fixed（已修复）/ waived（人工接受风险）。

    waived 代表接受风险上线，需 review:approve；fixed 需 review:submit。
    """
    frepo = RiskFindingRepository(db)
    finding = frepo.get(finding_id)
    if finding is None:
        raise BusinessError("风险发现不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    work = db.query(models.Work).filter(
        models.Work.id == finding.work_id).first()
    if work is None:
        raise BusinessError("风险发现不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    need = ("review:approve" if body.to_status == "waived"
            else "review:submit")
    _check_member(db, user, work.org_id, need)
    try:
        finding = frepo.resolve(finding_id, body.to_status,
                                resolved_by=user.id)
    except IllegalFindingTransition as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=422)
    db.commit()
    return success(_finding_out(finding))


# ---------------------------------------------------------------- 豁免

@router.post("/{work_id}/waivers",
             response_model=ApiResponse[dict])
def create_waiver_route(work_id: str, body: WaiverCreate,
                        ctx=Depends(require_work_access("review:approve")),
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """S6.5：创建交付豁免——原因和批准人必填，不可豁免项拒绝。"""
    work, ms = ctx
    try:
        w = create_waiver(db, work.org_id, work_id, body.gate_type,
                          body.check_id, body.reason, user.id,
                          body.expires_at)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=422)
    return success(w)


@router.get("/{work_id}/waivers",
            response_model=ApiResponse[dict])
def list_waivers_route(work_id: str,
                       include_revoked: bool = Query(False),
                       ctx=Depends(require_work_access("work:read")),
                       db: Session = Depends(get_db)):
    """豁免列表（含已撤销的可选）。"""
    items = list_waivers(db, work_id, include_revoked=include_revoked)
    return success({"items": items, "total": len(items)})


@waiver_router.delete("/{waiver_id}",
                      response_model=ApiResponse[dict])
def revoke_waiver_route(waiver_id: str,
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """撤销豁免（需 review:approve）。"""
    w = db.get(models.Waiver, waiver_id)
    if w is None:
        raise BusinessError("豁免不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    work = db.query(models.Work).filter(
        models.Work.id == w.work_id).first()
    if work is None:
        raise BusinessError("豁免不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    _check_member(db, user, work.org_id, "review:approve")
    return success(revoke_waiver(db, waiver_id))


# ---------------------------------------------------------------- 门禁配置

@delivery_router.get("/delivery/gates/{gate_type}",
                     response_model=ApiResponse[dict])
def get_gates(gate_type: Literal["plan", "production", "final"],
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """S6.5：查询某交付类型的门禁配置。"""
    return success({"gate_type": gate_type,
                    "items": get_gate_configs(db, gate_type)})


@delivery_router.patch("/delivery/gates/{gate_type}",
                       response_model=ApiResponse[dict])
def patch_gate(gate_type: Literal["plan", "production", "final"],
               body: GateConfigUpdate,
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """S6.5：调整门禁配置——仅组织管理员（owner/admin）可操作。"""
    is_admin = bool(db.query(models.Membership).filter(
        models.Membership.user_id == user.id,
        models.Membership.role.in_([ROLE_OWNER, ROLE_ADMIN])).first())
    if not is_admin:
        raise AuthError("仅组织管理员可调整交付门禁",
                        code=error_codes.PERMISSION_DENIED,
                        forbidden=True)
    cfg = upsert_gate_config(db, gate_type, body.check_id,
                             is_blocking=body.is_blocking,
                             is_waivable=body.is_waivable,
                             updated_by=user.id)
    return success(cfg)


@router.post("/{work_id}/delivery-check",
             response_model=ApiResponse[dict])
def v1_delivery_check(work_id: str, body: V1DeliveryCheckRequest,
                      ctx=Depends(require_work_access("work:read")),
                      db: Session = Depends(get_db)):
    """S6.5：配置化交付检查——门禁走 delivery_gate_configs，有效豁免参与计算。

    请求体字段缺省时，尝试从作品 data_json 中解析对应数据。
    """
    work, ms = ctx
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    storyboard = (body.storyboard if body.storyboard is not None
                  else data.get("storyboard"))
    product = (body.product if body.product is not None
               else data.get("product"))
    facts = body.facts or data.get("facts") or []
    feasibility = (body.feasibility if body.feasibility is not None
                   else data.get("feasibility"))
    shoot_state = body.shoot_state or data.get("shoot_state") or {}
    gate_type = body.check_type  # plan/final 与门禁类型同名
    configs = get_gate_configs(db, gate_type)
    waivers = get_active_waivers(db, work_id, gate_type)
    result = check_delivery(
        storyboard=storyboard, product=product, facts=facts,
        feasibility=feasibility, shoot_state=shoot_state,
        check_type=body.check_type,
        gate_configs=configs, waivers=waivers,
    )
    return success(result)
