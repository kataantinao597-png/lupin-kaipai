"""审核 Repository（032/201-205）。

- 201：审核状态机 pending/in_review/changes_requested/approved/rejected/expired。
- 202：高风险内容必须由人工批准——transition() 只做状态流转合法性检查，
  权限检查由 API 层（require_work_access + reviewer 角色）负责。
- 203：审核结论绑定 content_hash；内容变更后自动 expire_stale()。
- 204：AI 审核失败不得默认通过——本层不设"默认通过"路径。
- 205：decisions_json 记录每项风险的处置结果。
- 全部写操作走 @transactional_method（032）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    REVIEW_STATUSES,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_EXPIRED,
    REVIEW_STATUS_IN_REVIEW,
    REVIEW_STATUS_PENDING,
    REVIEW_TRANSITIONS,
    Review,
)
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IllegalReviewTransition(Exception):
    """032：非法审核状态流转。"""

    def __init__(self, review_id: str, from_status: str, to_status: str):
        self.review_id = review_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Review({review_id}) 非法流转：{from_status} -> {to_status}"
        )


def validate_review_transition(from_status: str, to_status: str) -> None:
    """032：校验审核状态流转合法性，非法抛 IllegalReviewTransition。"""
    allowed = REVIEW_TRANSITIONS.get(from_status, ())
    if to_status not in allowed:
        raise IllegalReviewTransition("?", from_status, to_status)


class ReviewRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, work_id: str, content_hash: str,
               storyboard_id: str | None = None,
               created_by: str = "",
               rule_version: str = "",
               model_version: str = "",
               prompt_version: str = "") -> Review:
        """创建一条待提交审核（pending）。

        S6.3：同时绑定规则/模型/Prompt 版本。
        """
        review = Review(
            org_id=org_id, work_id=work_id, storyboard_id=storyboard_id,
            content_hash=content_hash,
            rule_version=rule_version, model_version=model_version,
            prompt_version=prompt_version,
        )
        # created_by 记录在 reviewer_id 占位？不——创建人不等于审核人，
        # 保留 reviewer_id 给实际审核人。这里不写。
        self.db.add(review)
        self.db.flush()
        self.db.refresh(review)
        return review

    def get(self, review_id: str) -> Review | None:
        return self.db.get(Review, review_id)

    def latest_for_work(self, work_id: str) -> Review | None:
        """取作品最新一条审核记录（审计历史保留在表里）。"""
        return self.db.scalar(
            select(Review).where(Review.work_id == work_id)
            .order_by(Review.created_at.desc()).limit(1))

    def list_by_work(self, work_id: str, limit: int = 50) -> list[Review]:
        return list(self.db.scalars(
            select(Review).where(Review.work_id == work_id)
            .order_by(Review.created_at.desc()).limit(limit)))

    @transactional_method
    def transition(self, review_id: str, to_status: str,
                   expected_version: int,
                   reviewer_id: str | None = None,
                   decisions: dict[str, str] | None = None,
                   comment: str = "") -> Review:
        """032：审核状态流转——事务内校验 version + 合法流转，原子提交。

        202：流转到 approved 必须由 API 层先验 reviewer 权限。
        """
        if to_status not in REVIEW_STATUSES:
            raise ValueError(f"非法审核状态: {to_status}")
        review = self.get(review_id)
        if review is None:
            raise EntityNotFound("Review", review_id)
        if review.version != expected_version:
            raise OptimisticLockError("Review", review_id,
                                      expected_version, review.version)
        allowed = REVIEW_TRANSITIONS.get(review.status, ())
        if to_status not in allowed:
            raise IllegalReviewTransition(review_id, review.status, to_status)
        values: dict[str, Any] = {
            "status": to_status,
            "version": Review.version + 1,
            "updated_at": _utcnow(),
        }
        if reviewer_id is not None:
            values["reviewer_id"] = reviewer_id
            values["reviewed_at"] = _utcnow()
        if decisions is not None:
            values["decisions_json"] = decisions
        if comment:
            values["comment"] = comment
        result = self.db.execute(
            update(Review)
            .where(Review.id == review_id, Review.version == expected_version)
            .values(**values))
        if result.rowcount == 0:
            # 并发抢跑：version 已被别人改掉
            raise OptimisticLockError("Review", review_id,
                                      expected_version, None)
        self.db.flush()
        review = self.get(review_id)
        assert review is not None
        return review

    @transactional_method
    def expire_stale(self, work_id: str, current_content_hash: str) -> int:
        """203：内容变更后，把绑定旧哈希的"有效"审核自动置为 expired。

        返回失效的条数。事务内完成：查 + 改原子提交。
        """
        stale = list(self.db.scalars(
            select(Review).where(
                Review.work_id == work_id,
                Review.content_hash != current_content_hash,
                Review.status.in_([REVIEW_STATUS_PENDING,
                                   REVIEW_STATUS_IN_REVIEW,
                                   REVIEW_STATUS_APPROVED]))))
        count = 0
        for r in stale:
            # 只有允许的流转才做；approved->expired 合法，pending->expired 合法
            if REVIEW_STATUS_EXPIRED in REVIEW_TRANSITIONS.get(r.status, ()):
                r.status = REVIEW_STATUS_EXPIRED
                r.version = r.version + 1
                r.updated_at = _utcnow()
                count += 1
        self.db.flush()
        return count

    @transactional_method
    def expire_all_for_org(self, org_id: str) -> int:
        """S6.3/FactChanged：组织事实库发生实质变更时，保守地将该组织下
        所有"有效"审核置为 expired（只做过期提示，不覆盖旧成果）。

        返回失效的条数。事务内完成。
        """
        stale = list(self.db.scalars(
            select(Review).where(
                Review.org_id == org_id,
                Review.status.in_([REVIEW_STATUS_PENDING,
                                   REVIEW_STATUS_IN_REVIEW,
                                   REVIEW_STATUS_APPROVED]))))
        count = 0
        for r in stale:
            if REVIEW_STATUS_EXPIRED in REVIEW_TRANSITIONS.get(r.status, ()):
                r.status = REVIEW_STATUS_EXPIRED
                r.version = (r.version or 1) + 1
                r.updated_at = _utcnow()
                count += 1
        self.db.flush()
        return count
