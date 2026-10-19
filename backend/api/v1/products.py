"""V3.0 B-1：产品档案 API（跨项目复用的产品资料中心）。

- POST   /api/v1/products                  创建产品档案（需 product:create）
- GET    /api/v1/products?org_id=          产品列表（需 product:read）
- GET    /api/v1/products/{id}?org_id=      产品详情（需 product:read）
- PATCH  /api/v1/products/{id}             更新档案（需 product:update；
                                          expected_version 必填，冲突 → 409）
- DELETE /api/v1/products/{id}?org_id=      删除档案（需 product:delete；
                                          仍有事实引用 → 409，硬删）

code 组织内唯一，创建后不可改（是 facts 引用的稳定标识）。
facts.product_id 自由字符串可引用产品 id 或 code（B-2 兼容）；
删除产品时任一命中都视为有关联引用。
"""
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import get_membership, has_permission, assert_can
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...repositories.base import EntityNotFound, OptimisticLockError
from ...repositories.product_repository import (
    ProductCodeConflictError,
    ProductHasFactsError,
    ProductRepository,
)
from .schemas import ProductCreate, ProductOut, ProductUpdate

router = APIRouter(prefix="/products", tags=["v1-products"])


def _product_out(p: models.Product) -> ProductOut:
    points = p.selling_points
    if not isinstance(points, list):
        points = []
    return ProductOut(
        id=p.id, org_id=p.org_id, code=p.code or "",
        name=p.name or "", category=p.category or "",
        origin=p.origin or "",
        brand_profile=p.brand_profile or "",
        brand_tone=p.brand_tone or "",
        selling_points=[str(x) for x in points],
        ad_restrictions=p.ad_restrictions or "",
        no_expand_claims=p.no_expand_claims or "",
        version=p.version or 1,
        created_at=p.created_at, updated_at=p.updated_at,
    )


def _require_product_perm(db: Session, user: User, org_id: str,
                          action: str, what: str):
    """取成员身份并校验权限；无权一律 404（防枚举，与 facts.py 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)  # 050 二次验证
    return ms


@router.post("", response_model=ApiResponse[ProductOut],
             status_code=status.HTTP_201_CREATED)
def create_product(
    req: ProductCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建产品档案（code 组织内唯一）。需要 product:create 权限。"""
    _require_product_perm(db, user, req.org_id, "product:create", "创建产品档案")
    repo = ProductRepository(db)
    try:
        product = repo.create(
            org_id=req.org_id, code=req.code, name=req.name,
            category=req.category, origin=req.origin,
            brand_profile=req.brand_profile, brand_tone=req.brand_tone,
            selling_points=req.selling_points,
            ad_restrictions=req.ad_restrictions,
            no_expand_claims=req.no_expand_claims,
        )
    except ProductCodeConflictError as e:
        raise BusinessError(str(e), code=error_codes.PRODUCT_CODE_CONFLICT,
                            http_status=409)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success(_product_out(product))


@router.get("", response_model=ApiResponse[list[ProductOut]])
def list_products(
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """产品档案列表（时间倒序）。需要 product:read 权限。"""
    _require_product_perm(db, user, org_id, "product:read", "查看产品档案")
    products = ProductRepository(db).list_by_org(org_id)
    return success([_product_out(p) for p in products])


@router.get("/{product_id}", response_model=ApiResponse[ProductOut])
def get_product(
    product_id: str,
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """产品档案详情。需要 product:read 权限。"""
    _require_product_perm(db, user, org_id, "product:read", "查看产品档案")
    product = ProductRepository(db).get(product_id, org_id)
    if product is None:
        raise BusinessError("产品档案不存在",
                            code=error_codes.PRODUCT_NOT_FOUND, http_status=404)
    return success(_product_out(product))


@router.patch("/{product_id}", response_model=ApiResponse[ProductOut])
def update_product(
    product_id: str,
    req: ProductUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新产品档案（code 不可改）。需要 product:update 权限；
    expected_version 必填，版本冲突 → 409。"""
    _require_product_perm(db, user, req.org_id, "product:update", "修改产品档案")
    repo = ProductRepository(db)
    fields = req.model_dump(exclude={"org_id", "expected_version"})
    try:
        product = repo.update(product_id, req.org_id,
                              req.expected_version, **fields)
    except EntityNotFound:
        raise BusinessError("产品档案不存在",
                            code=error_codes.PRODUCT_NOT_FOUND, http_status=404)
    except OptimisticLockError as e:
        raise BusinessError(
            f"产品档案已被他人修改（期望版本 {e.expected}，当前 {e.actual}），"
            "请刷新后重试",
            code=error_codes.VERSION_CONFLICT, http_status=409)
    return success(_product_out(product))


@router.delete("/{product_id}", response_model=ApiResponse[dict])
def delete_product(
    product_id: str,
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除产品档案（硬删）。需要 product:delete 权限；
    仍有事实引用（product_id 命中本产品 id 或 code）→ 409。"""
    _require_product_perm(db, user, org_id, "product:delete", "删除产品档案")
    repo = ProductRepository(db)
    try:
        repo.delete(product_id, org_id)
    except EntityNotFound:
        raise BusinessError("产品档案不存在",
                            code=error_codes.PRODUCT_NOT_FOUND, http_status=404)
    except ProductHasFactsError as e:
        raise BusinessError(str(e), code=error_codes.PRODUCT_HAS_FACTS,
                            http_status=409)
    return success({"deleted": True, "id": product_id})
