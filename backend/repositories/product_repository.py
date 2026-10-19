"""V3.0 B-1：产品档案 Repository。

- Product 是跨项目复用的产品资料中心；facts.product_id 自由字符串
  可引用产品 id 或 code（B-2 兼容），此处不做 DB 级外键。
- 删除产品前检查 facts 引用：任一 fact 的 product_id 命中本产品
  的 id 或 code 即视为有关联，抛 ProductHasFactsError。
- code 组织内唯一，创建时检查 + DB 唯一约束双保险；code 创建后
  不可修改（是引用关系的稳定标识）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Fact, Product, ProductRef
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProductCodeConflictError(ValueError):
    """同一组织内产品编号重复。"""


class ProductHasFactsError(ValueError):
    """仍有事实引用该产品，禁止删除。"""


# PATCH 允许更新的字段（code 创建后不可改，是引用稳定标识）
UPDATABLE_FIELDS = (
    "name", "category", "origin", "brand_profile", "brand_tone",
    "selling_points", "ad_restrictions", "no_expand_claims",
)


class ProductRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, code: str, name: str,
               category: str = "", origin: str = "",
               brand_profile: str = "", brand_tone: str = "",
               selling_points: list[str] | None = None,
               ad_restrictions: str = "",
               no_expand_claims: str = "") -> Product:
        """创建产品档案。code 在组织内必须唯一，重复抛
        ProductCodeConflictError。name 必填。"""
        code = (code or "").strip()
        name = (name or "").strip()
        if not code:
            raise ValueError("产品编号 code 不能为空")
        if not name:
            raise ValueError("产品名称 name 不能为空")
        if self.get_by_code(org_id, code) is not None:
            raise ProductCodeConflictError(
                f"组织内产品编号已存在: {code}")
        product = Product(
            org_id=org_id, code=code, name=name,
            category=(category or "").strip(),
            origin=(origin or "").strip(),
            brand_profile=brand_profile or "",
            brand_tone=(brand_tone or "").strip(),
            selling_points=list(selling_points or []),
            ad_restrictions=ad_restrictions or "",
            no_expand_claims=no_expand_claims or "",
        )
        self.db.add(product)
        self.db.flush()
        self.db.refresh(product)
        return product

    def get(self, product_id: str, org_id: str) -> Product | None:
        return self.db.scalar(
            select(Product).where(Product.id == product_id,
                                  Product.org_id == org_id))

    def get_by_code(self, org_id: str, code: str) -> Product | None:
        return self.db.scalar(
            select(Product).where(Product.org_id == org_id,
                                  Product.code == code))

    def list_by_org(self, org_id: str,
                    limit: int = 200, offset: int = 0) -> list[Product]:
        return list(self.db.scalars(
            select(Product).where(Product.org_id == org_id)
            .order_by(Product.updated_at.desc())
            .limit(limit).offset(offset)))

    def count_by_org(self, org_id: str) -> int:
        return self.db.scalar(
            select(func.count()).select_from(Product).where(
                Product.org_id == org_id)) or 0

    @transactional_method
    def update(self, product_id: str, org_id: str,
               expected_version: int,
               **fields: Any) -> Product:
        """更新产品档案（code 不可改）。expected_version 必须匹配，
        冲突抛 OptimisticLockError；产品不存在抛 EntityNotFound。"""
        product = self.get(product_id, org_id)
        if product is None:
            raise EntityNotFound("Product", product_id)
        values: dict[str, Any] = {}
        for key in UPDATABLE_FIELDS:
            if key not in fields or fields[key] is None:
                continue
            if key == "selling_points":
                values[key] = list(fields[key] or [])
            elif key in ("brand_profile", "ad_restrictions",
                         "no_expand_claims"):
                values[key] = fields[key] or ""
            else:
                values[key] = (fields[key] or "").strip()
        if values:
            values["version"] = Product.version + 1
            values["updated_at"] = _utcnow()
            result = self.db.execute(
                update(Product)
                .where(Product.id == product_id, Product.org_id == org_id,
                       Product.version == expected_version)
                .values(**values))
            if result.rowcount == 0:
                cur = self.get(product_id, org_id)
                if cur is None:
                    raise EntityNotFound("Product", product_id)
                raise OptimisticLockError("Product", product_id,
                                          expected_version, cur.version)
            self.db.flush()
            product = self.get(product_id, org_id)
            assert product is not None
        return product

    def count_referencing_facts(self, product: Product) -> int:
        """引用该产品的事实数量。

        facts.product_id 是自由字符串（B-2 兼容）：命中产品 id
        或 code 任一都算引用。
        """
        return self.db.scalar(
            select(func.count()).select_from(Fact).where(
                Fact.org_id == product.org_id,
                Fact.product_id.in_([product.id, product.code]))) or 0

    @transactional_method
    def delete(self, product_id: str, org_id: str) -> None:
        """删除产品档案。仍有事实引用（id 或 code 任一命中）时抛
        ProductHasFactsError；不存在抛 EntityNotFound。硬删。"""
        product = self.get(product_id, org_id)
        if product is None:
            raise EntityNotFound("Product", product_id)
        n = self.count_referencing_facts(product)
        if n > 0:
            raise ProductHasFactsError(
                f"该产品仍有 {n} 条事实引用，无法删除")
        self.db.delete(product)
        self.db.flush()

    # ---------- V3.0 B-5：引用标记 ----------

    def sync_work_ref(self, work_id: str, product: Product,
                      product_version: int) -> None:
        """作品保存时同步产品引用（一个作品最多一条引用记录）。

        - product_version=None 表示前端清除了引用 → 删除旧记录。
        - recorded_version < 产品当前 version 时直接标为 stale（避免
          重新保存把"已过期"标记洗掉）。
        - 只做标记，不读写作品内容本身。
        """
        self.db.query(ProductRef).filter(
            ProductRef.ref_type == "work",
            ProductRef.ref_id == work_id).delete()
        if product is None:
            self.db.flush()
            return
        cur_ver = product.version or 1
        self.db.add(ProductRef(
            org_id=product.org_id, product_id=product.id,
            ref_type="work", ref_id=work_id,
            product_version=product_version,
            product_code=product.code, product_name=product.name,
            stale=bool(product_version < cur_ver)))
        self.db.flush()

    @transactional_method
    def mark_refs_stale(self, product: Product, new_version: int) -> int:
        """产品资料更新后：把引用旧版本的记录标为 stale。

        返回被标记的条数。只标记不改写任何内容。
        """
        result = self.db.execute(
            update(ProductRef)
            .where(ProductRef.product_id == product.id,
                   ProductRef.product_version < new_version,
                   ProductRef.stale.is_(False))
            .values(stale=True, updated_at=_utcnow()))
        self.db.flush()
        return result.rowcount or 0

    def is_ref_stale(self, ref_type: str, ref_id: str) -> bool:
        """某条引用是否为旧版本（供作品详情展示提示）。"""
        return self.db.scalar(
            select(func.count()).select_from(ProductRef).where(
                ProductRef.ref_type == ref_type,
                ProductRef.ref_id == ref_id,
                ProductRef.stale.is_(True))) > 0
