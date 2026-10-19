"""V3.0 F：发布中心 Repository。

- PublishRecord：一次独立的发布任务。
- 状态流转只做存在性校验；合法转换由 API 层的 PUBLISH_TRANSITIONS 强制。
- 关联字段（work/copy_version/platform_version/asset）只记录引用，
  不级联、不复制文件；删除引用关系 ≠ 删除实体文件。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .base import EntityNotFound, transactional_method


class PublishRecordNotFoundError(EntityNotFound):
    """发布记录不存在或不属于该组织。"""

    def __init__(self, rec_id: str):
        super().__init__("发布记录", rec_id)


class PublishRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, title: str, platform: str,
               work_id: str | None = None,
               copy_version_id: str | None = None,
               platform_version_id: str | None = None,
               video_asset_id: str | None = None,
               cover_asset_id: str | None = None,
               created_by: str = "") -> models.PublishRecord:
        rec = models.PublishRecord(
            org_id=org_id, title=(title or "").strip() or "未命名发布",
            platform=(platform or "").strip(),
            work_id=work_id or None,
            copy_version_id=copy_version_id or None,
            platform_version_id=platform_version_id or None,
            video_asset_id=video_asset_id or None,
            cover_asset_id=cover_asset_id or None,
            created_by=created_by or "")
        self.db.add(rec)
        self.db.flush()
        return rec

    def get(self, org_id: str, rec_id: str) -> models.PublishRecord:
        rec = self.db.execute(
            select(models.PublishRecord).where(
                models.PublishRecord.id == rec_id,
                models.PublishRecord.org_id == org_id)
        ).scalar_one_or_none()
        if rec is None:
            raise PublishRecordNotFoundError(f"发布记录不存在: {rec_id}")
        return rec

    def list(self, org_id: str, status: str | None = None,
             limit: int = 50) -> list[models.PublishRecord]:
        q = select(models.PublishRecord).where(
            models.PublishRecord.org_id == org_id)
        if status:
            q = q.where(models.PublishRecord.status == status)
        q = q.order_by(models.PublishRecord.updated_at.desc()).limit(limit)
        return list(self.db.execute(q).scalars().all())

    @transactional_method
    def update_fields(self, rec: models.PublishRecord,
                      **fields) -> models.PublishRecord:
        for k, v in fields.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        self.db.flush()
        return rec

    @transactional_method
    def delete(self, org_id: str, rec_id: str) -> None:
        rec = self.get(org_id, rec_id)
        self.db.delete(rec)
