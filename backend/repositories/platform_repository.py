"""V3.0 E：多平台版本 Repository。

- PlatformVersion：各平台版本独立行；修改一个平台版本只 update 该行，
  绝不触及其他平台版本。
- revision_no 在 (doc_id, platform) 内单调递增，由 next_revision_no() 分配。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .base import EntityNotFound, transactional_method


class PlatformVersionNotFoundError(EntityNotFound):
    """平台版本不存在或不属于该组织。"""

    def __init__(self, pv_id: str):
        super().__init__("平台版本", pv_id)


class PlatformRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, doc_id: str,
               copy_version_id: str | None,
               source_version_no: int,
               platform: str,
               created_by: str = "", **fields) -> models.PlatformVersion:
        if platform not in models.PLATFORM_IDS:
            raise ValueError(f"不支持的平台: {platform}")
        pv = models.PlatformVersion(
            org_id=org_id, doc_id=doc_id,
            copy_version_id=copy_version_id,
            source_version_no=source_version_no,
            platform=platform,
            revision_no=self.next_revision_no(doc_id, platform),
            created_by=created_by or "", **fields)
        self.db.add(pv)
        self.db.flush()
        return pv

    def next_revision_no(self, doc_id: str, platform: str) -> int:
        q = select(func.max(models.PlatformVersion.revision_no)).where(
            models.PlatformVersion.doc_id == doc_id,
            models.PlatformVersion.platform == platform)
        mx = self.db.execute(q).scalar()
        return (mx or 0) + 1

    def get(self, org_id: str, pv_id: str) -> models.PlatformVersion:
        pv = self.db.execute(
            select(models.PlatformVersion).where(
                models.PlatformVersion.id == pv_id,
                models.PlatformVersion.org_id == org_id)
        ).scalar_one_or_none()
        if pv is None:
            raise PlatformVersionNotFoundError(pv_id)
        return pv

    def list_by_doc(self, org_id: str, doc_id: str,
                    platform: str | None = None,
                    limit: int = 200) -> list[models.PlatformVersion]:
        q = select(models.PlatformVersion).where(
            models.PlatformVersion.org_id == org_id,
            models.PlatformVersion.doc_id == doc_id)
        if platform:
            q = q.where(models.PlatformVersion.platform == platform)
        q = q.order_by(models.PlatformVersion.platform,
                       models.PlatformVersion.revision_no.desc()).limit(limit)
        return list(self.db.execute(q).scalars().all())

    @transactional_method
    def update(self, org_id: str, pv_id: str,
               **fields) -> models.PlatformVersion:
        """只更新本行；调用方限定可改字段。"""
        pv = self.get(org_id, pv_id)
        allowed = {"title", "body", "hashtags", "cover_text",
                   "pinned_comment", "cta", "review_status"}
        for k, v in fields.items():
            if k in allowed:
                setattr(pv, k, v)
        self.db.flush()
        return pv

    @transactional_method
    def delete(self, org_id: str, pv_id: str) -> None:
        pv = self.get(org_id, pv_id)
        self.db.delete(pv)
        self.db.flush()
