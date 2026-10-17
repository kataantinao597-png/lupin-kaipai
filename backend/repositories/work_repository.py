"""作品 Repository（083/087/088）。

- 全部查询默认过滤 deleted_at IS NULL（软删除作品不可见）。
- update() 支持 expected_version：提供时做乐观锁检查（088），
  version 匹配才更新并 +1，不匹配抛 OptimisticLockError。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Work
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, owner_id: str, title: str = "",
               data: dict[str, Any] | None = None,
               status: str = "draft") -> Work:
        work = Work(
            org_id=org_id, owner_id=owner_id, title=title.strip(),
            data_json=json.dumps(data or {}, ensure_ascii=False),
            status=status,
        )
        self.db.add(work)
        self.db.flush()
        self.db.refresh(work)
        return work

    def get(self, work_id: str, org_id: str) -> Work | None:
        """048：查询强制带 org 上下文；软删除的作品不可见。"""
        return self.db.scalar(
            select(Work).where(
                Work.id == work_id,
                Work.org_id == org_id,
                Work.deleted_at.is_(None),
            )
        )

    def list_by_org(self, org_id: str, include_archived: bool = False,
                    limit: int = 100, offset: int = 0) -> list[Work]:
        stmt = select(Work).where(
            Work.org_id == org_id, Work.deleted_at.is_(None))
        if not include_archived:
            stmt = stmt.where(Work.status != "archived")
        return list(self.db.scalars(
            stmt.order_by(Work.updated_at.desc()).limit(limit).offset(offset)))

    def _current_version(self, work_id: str, org_id: str) -> int | None:
        return self.db.scalar(
            select(Work.version).where(
                Work.id == work_id, Work.org_id == org_id,
                Work.deleted_at.is_(None)))

    @transactional_method
    def update(self, work_id: str, org_id: str,
               expected_version: int | None = None,
               title: str | None = None,
               data: dict[str, Any] | None = None,
               status: str | None = None) -> Work:
        """088：expected_version 非空时做乐观锁检查。"""
        if status is not None and status not in ("draft", "active", "archived"):
            raise ValueError(f"非法 status: {status}")
        values: dict[str, Any] = {"updated_at": _utcnow()}
        if title is not None:
            values["title"] = title.strip()
        if data is not None:
            values["data_json"] = json.dumps(data, ensure_ascii=False)
        if status is not None:
            values["status"] = status

        if expected_version is not None:
            values["version"] = Work.version + 1
            result = self.db.execute(
                update(Work)
                .where(Work.id == work_id, Work.org_id == org_id,
                       Work.deleted_at.is_(None),
                       Work.version == expected_version)
                .values(**values)
            )
            if result.rowcount == 0:
                actual = self._current_version(work_id, org_id)
                if actual is None:
                    raise EntityNotFound("Work", work_id)
                raise OptimisticLockError("Work", work_id,
                                          expected_version, actual)
        else:
            # 兼容老调用：不带版本时直接更新（不做冲突检查）
            result = self.db.execute(
                update(Work)
                .where(Work.id == work_id, Work.org_id == org_id,
                       Work.deleted_at.is_(None))
                .values(**values)
            )
            if result.rowcount == 0:
                raise EntityNotFound("Work", work_id)
        self.db.flush()
        work = self.get(work_id, org_id)
        assert work is not None
        return work

    @transactional_method
    def soft_delete(self, work_id: str, org_id: str,
                    expected_version: int | None = None) -> None:
        """083：软删除——写 deleted_at，数据保留可恢复。"""
        if expected_version is not None:
            result = self.db.execute(
                update(Work)
                .where(Work.id == work_id, Work.org_id == org_id,
                       Work.deleted_at.is_(None),
                       Work.version == expected_version)
                .values(deleted_at=_utcnow(), version=Work.version + 1,
                        updated_at=_utcnow())
            )
            if result.rowcount == 0:
                actual = self._current_version(work_id, org_id)
                if actual is None:
                    raise EntityNotFound("Work", work_id)
                raise OptimisticLockError("Work", work_id,
                                          expected_version, actual)
        else:
            result = self.db.execute(
                update(Work)
                .where(Work.id == work_id, Work.org_id == org_id,
                       Work.deleted_at.is_(None))
                .values(deleted_at=_utcnow(), updated_at=_utcnow())
            )
            if result.rowcount == 0:
                raise EntityNotFound("Work", work_id)

    def count_by_org(self, org_id: str) -> int:
        return self.db.scalar(
            select(func.count()).select_from(Work).where(
                Work.org_id == org_id, Work.deleted_at.is_(None))) or 0

    # ---------- 025：领域实体返回（示例） ----------

    def get_domain(self, work_id: str, org_id: str):
        """025 示例：返回领域实体而非 ORM 对象。

        services/api 层应使用此方法；旧 get() 保留兼容，逐步迁移。
        """
        from ..domain.converters import work_to_domain
        orm = self.get(work_id, org_id)
        return work_to_domain(orm) if orm else None

    def list_domain_by_org(self, org_id: str, include_archived: bool = False,
                           limit: int = 100, offset: int = 0):
        """025 示例：批量返回领域实体。"""
        from ..domain.converters import work_to_domain
        return [work_to_domain(w) for w in
                self.list_by_org(org_id, include_archived, limit, offset)]

    # TODO(025)：FactRepository.get_domain / StoryboardRepository.get_domain
    # 待补；届时 services 层统一切换到领域实体。
