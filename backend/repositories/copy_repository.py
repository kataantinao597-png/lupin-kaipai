"""V3.0 D：文案优化器 Repository。

- CopyDoc / CopyVersion 只读追加：任何"修改"都是新增版本行，
  旧行不 update 不 delete（除了删整个文档级联）。
- version_no 在 doc 内单调递增，由 next_version_no() 分配。
- 当前有效版本 = status="confirmed" 且 version_no 最大的行。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .base import EntityNotFound, transactional_method


class CopyDocNotFoundError(EntityNotFound):
    """文案文档不存在或不属于该组织。"""

    def __init__(self, doc_id: str):
        super().__init__("文案文档", doc_id)


class CopyVersionNotFoundError(EntityNotFound):
    """文案版本不存在或不属于该文档。"""

    def __init__(self, version_id: str):
        super().__init__("文案版本", version_id)


class CopyRepository:
    def __init__(self, db: Session):
        self.db = db

    # ---------- 文档 ----------

    @transactional_method
    def create_doc(self, org_id: str, title: str, kind: str,
                   product_id: str | None = None,
                   created_by: str = "") -> models.CopyDoc:
        if kind not in models.COPY_KINDS:
            raise ValueError(f"不支持的文案类型: {kind}")
        doc = models.CopyDoc(
            org_id=org_id, title=(title or "").strip() or "未命名文案",
            kind=kind, product_id=product_id or None,
            created_by=created_by or "")
        self.db.add(doc)
        self.db.flush()
        return doc

    def get_doc(self, org_id: str, doc_id: str) -> models.CopyDoc:
        doc = self.db.execute(
            select(models.CopyDoc).where(
                models.CopyDoc.id == doc_id,
                models.CopyDoc.org_id == org_id)
        ).scalar_one_or_none()
        if doc is None:
            raise CopyDocNotFoundError(f"文案文档不存在: {doc_id}")
        return doc

    def list_docs(self, org_id: str, kind: str | None = None,
                  limit: int = 50) -> list[models.CopyDoc]:
        q = select(models.CopyDoc).where(
            models.CopyDoc.org_id == org_id)
        if kind:
            q = q.where(models.CopyDoc.kind == kind)
        q = q.order_by(models.CopyDoc.updated_at.desc()).limit(limit)
        return list(self.db.execute(q).scalars().all())

    @transactional_method
    def delete_doc(self, org_id: str, doc_id: str) -> None:
        doc = self.get_doc(org_id, doc_id)
        self.db.delete(doc)  # 版本行 CASCADE

    # ---------- 版本 ----------

    def next_version_no(self, doc_id: str) -> int:
        mx = self.db.execute(
            select(func.max(models.CopyVersion.version_no)).where(
                models.CopyVersion.doc_id == doc_id)
        ).scalar()
        return (mx or 0) + 1

    @transactional_method
    def add_version(self, org_id: str, doc_id: str, **fields) -> models.CopyVersion:
        """新增版本行（只增不改）。version_no 自动分配。"""
        self.get_doc(org_id, doc_id)  # 校验归属
        v = models.CopyVersion(
            org_id=org_id, doc_id=doc_id,
            version_no=self.next_version_no(doc_id),
            **fields)
        self.db.add(v)
        self.db.flush()
        return v

    def get_version(self, org_id: str, doc_id: str,
                    version_id: str) -> models.CopyVersion:
        v = self.db.execute(
            select(models.CopyVersion).where(
                models.CopyVersion.id == version_id,
                models.CopyVersion.doc_id == doc_id,
                models.CopyVersion.org_id == org_id)
        ).scalar_one_or_none()
        if v is None:
            raise CopyVersionNotFoundError(f"文案版本不存在: {version_id}")
        return v

    def list_versions(self, org_id: str,
                      doc_id: str) -> list[models.CopyVersion]:
        self.get_doc(org_id, doc_id)
        q = select(models.CopyVersion).where(
            models.CopyVersion.doc_id == doc_id,
            models.CopyVersion.org_id == org_id,
        ).order_by(models.CopyVersion.version_no.asc())
        return list(self.db.execute(q).scalars().all())

    def current_version(self, org_id: str,
                        doc_id: str) -> models.CopyVersion | None:
        """当前有效版本：已确认且 version_no 最大的行；无则 None。"""
        q = select(models.CopyVersion).where(
            models.CopyVersion.doc_id == doc_id,
            models.CopyVersion.org_id == org_id,
            models.CopyVersion.status == models.COPY_STATUS_CONFIRMED,
        ).order_by(models.CopyVersion.version_no.desc()).limit(1)
        return self.db.execute(q).scalar_one_or_none()

    # ---------- O-05：列表批量聚合（list_docs 的 N+1 改 2 次查询） ----------
    # 只取聚合值，不拉版本全量行（含正文）：current_version_no 用
    # MAX(version_no) GROUP BY doc_id，version_count 用 COUNT(*) GROUP
    # BY doc_id。返回 dict 缺 key 即该文档无对应行（无已确认版本 /
    # 无任何版本），调用方按 None / 0 处理。

    def current_version_nos(self, org_id: str,
                            doc_ids: list[str]) -> dict[str, int]:
        """批量：每文档最大已确认 version_no。"""
        if not doc_ids:
            return {}
        rows = self.db.execute(
            select(models.CopyVersion.doc_id,
                   func.max(models.CopyVersion.version_no)).where(
                models.CopyVersion.org_id == org_id,
                models.CopyVersion.doc_id.in_(doc_ids),
                models.CopyVersion.status == models.COPY_STATUS_CONFIRMED,
            ).group_by(models.CopyVersion.doc_id)).all()
        return {doc_id: mx for doc_id, mx in rows}

    def version_counts(self, org_id: str,
                       doc_ids: list[str]) -> dict[str, int]:
        """批量：每文档版本总数。"""
        if not doc_ids:
            return {}
        rows = self.db.execute(
            select(models.CopyVersion.doc_id,
                   func.count()).where(
                models.CopyVersion.org_id == org_id,
                models.CopyVersion.doc_id.in_(doc_ids),
            ).group_by(models.CopyVersion.doc_id)).all()
        return {doc_id: n for doc_id, n in rows}

    @transactional_method
    def confirm_version(self, org_id: str, doc_id: str,
                        version_id: str) -> models.CopyVersion:
        """用户确认：将 draft 行标为 confirmed。旧 confirmed 行保留不动。"""
        v = self.get_version(org_id, doc_id, version_id)
        if v.status != models.COPY_STATUS_DRAFT:
            raise ValueError("只有草稿版本可以确认")
        v.status = models.COPY_STATUS_CONFIRMED
        self.db.flush()
        return v
