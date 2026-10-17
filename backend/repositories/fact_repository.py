"""事实库 Repository（085/087/088）。

- list_usable() 只返回 status=confirmed 且 allow_use=true 的事实（010/118），
  供 AI 生成时引用。
- set_status() 只能由人工/确定性规则调用，模型无权自升状态（143）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import FACT_STATUSES, Fact
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, content: str, product_id: str = "",
               category: str = "", source_type: str = "human_statement",
               source_ref: str = "",
               expires_at: datetime | None = None) -> Fact:
        fact = Fact(
            org_id=org_id, content=content.strip(), product_id=product_id,
            category=category, source_type=source_type, source_ref=source_ref,
            expires_at=expires_at,
        )
        self.db.add(fact)
        self.db.flush()
        self.db.refresh(fact)
        return fact

    def get(self, fact_id: str, org_id: str) -> Fact | None:
        return self.db.scalar(
            select(Fact).where(Fact.id == fact_id, Fact.org_id == org_id))

    def list_usable(self, org_id: str, product_id: str | None = None,
                    limit: int = 200) -> list[Fact]:
        """010/118：AI 可引用的事实——已确认且允许使用，且未过期。"""
        now = _utcnow()
        stmt = select(Fact).where(
            Fact.org_id == org_id,
            Fact.status == "confirmed",
            Fact.allow_use.is_(True),
        )
        if product_id is not None:
            stmt = stmt.where(Fact.product_id == product_id)
        stmt = stmt.where(
            (Fact.expires_at.is_(None)) | (Fact.expires_at > now))
        return list(self.db.scalars(
            stmt.order_by(Fact.updated_at.desc()).limit(limit)))

    def list_by_org(self, org_id: str, status: str | None = None,
                    limit: int = 200, offset: int = 0) -> list[Fact]:
        stmt = select(Fact).where(Fact.org_id == org_id)
        if status is not None:
            if status not in FACT_STATUSES:
                raise ValueError(f"非法事实状态: {status}")
            stmt = stmt.where(Fact.status == status)
        return list(self.db.scalars(
            stmt.order_by(Fact.updated_at.desc()).limit(limit).offset(offset)))

    @transactional_method
    def set_status(self, fact_id: str, org_id: str, status: str,
                   expected_version: int,
                   verified_by: str | None = None,
                   allow_use: bool | None = None) -> Fact:
        """088：状态流转必须带 version，冲突抛 OptimisticLockError。"""
        if status not in FACT_STATUSES:
            raise ValueError(f"非法事实状态: {status}")
        values: dict[str, Any] = {
            "status": status,
            "version": Fact.version + 1,
            "updated_at": _utcnow(),
        }
        if verified_by is not None:
            values["verified_by"] = verified_by
            values["verified_at"] = _utcnow()
        if allow_use is not None:
            values["allow_use"] = allow_use
        result = self.db.execute(
            update(Fact)
            .where(Fact.id == fact_id, Fact.org_id == org_id,
                   Fact.version == expected_version)
            .values(**values))
        if result.rowcount == 0:
            fact = self.get(fact_id, org_id)
            if fact is None:
                raise EntityNotFound("Fact", fact_id)
            raise OptimisticLockError("Fact", fact_id,
                                      expected_version, fact.version)
        self.db.flush()
        fact = self.get(fact_id, org_id)
        assert fact is not None
        return fact

    def count_by_org(self, org_id: str) -> int:
        return self.db.scalar(
            select(func.count()).select_from(Fact).where(
                Fact.org_id == org_id)) or 0
