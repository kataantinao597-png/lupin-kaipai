"""S6.4：风险发现追踪 Repository。

- 每条风险发现定位到镜头（shot_id 稳定 ID / shot_no）或文字片段。
- 状态机：open -> fixed / waived；fixed/waived 为终态（重新发现走新建）。
- 全部写操作走 @transactional_method（032 约定）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    FINDING_STATUSES,
    FINDING_STATUS_FIXED,
    FINDING_STATUS_OPEN,
    FINDING_STATUS_WAIVED,
    RiskFinding,
)
from .base import EntityNotFound, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IllegalFindingTransition(Exception):
    def __init__(self, finding_id: str, from_status: str, to_status: str):
        self.finding_id = finding_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"RiskFinding({finding_id}) 非法流转：{from_status} -> {to_status}")


_FINDING_TRANSITIONS: dict[str, tuple[str, ...]] = {
    FINDING_STATUS_OPEN: (FINDING_STATUS_FIXED, FINDING_STATUS_WAIVED),
    FINDING_STATUS_FIXED: (),
    FINDING_STATUS_WAIVED: (),
}


class RiskFindingRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, review_id: str, org_id: str, work_id: str,
               severity: str = "medium", source: str = "",
               reason: str = "", text_fragment: str | None = None,
               shot_id: str | None = None,
               shot_no: int | None = None) -> RiskFinding:
        """新建一条风险发现（open）。shot_id/text_fragment 至少其一。"""
        finding = RiskFinding(
            review_id=review_id, org_id=org_id, work_id=work_id,
            severity=severity, source=source, reason=reason,
            text_fragment=text_fragment, shot_id=shot_id, shot_no=shot_no,
        )
        self.db.add(finding)
        self.db.flush()
        self.db.refresh(finding)
        return finding

    def get(self, finding_id: str) -> RiskFinding | None:
        return self.db.get(RiskFinding, finding_id)

    def list_by_review(self, review_id: str,
                       status: str | None = None) -> list[RiskFinding]:
        q = (select(RiskFinding).where(RiskFinding.review_id == review_id)
             .order_by(RiskFinding.created_at.asc()))
        if status:
            q = q.where(RiskFinding.status == status)
        return list(self.db.scalars(q))

    def count_open(self, review_id: str) -> int:
        return int(self.db.scalar(
            select(func.count()).select_from(RiskFinding).where(
                RiskFinding.review_id == review_id,
                RiskFinding.status == FINDING_STATUS_OPEN)) or 0)

    @transactional_method
    def resolve(self, finding_id: str, to_status: str,
                resolved_by: str = "") -> RiskFinding:
        """S6.4：风险处置——open -> fixed（已修复）/ waived（人工接受风险）。

        waived 必须由调用方先验权限（人工审核决定），本层只做流转校验。
        """
        if to_status not in (FINDING_STATUS_FIXED, FINDING_STATUS_WAIVED):
            raise ValueError(f"非法处置状态: {to_status}")
        finding = self.get(finding_id)
        if finding is None:
            raise EntityNotFound("RiskFinding", finding_id)
        allowed = _FINDING_TRANSITIONS.get(finding.status, ())
        if to_status not in allowed:
            raise IllegalFindingTransition(finding_id, finding.status,
                                           to_status)
        finding.status = to_status
        finding.resolved_by = resolved_by or None
        finding.resolved_at = _utcnow()
        finding.updated_at = _utcnow()
        self.db.flush()
        self.db.refresh(finding)
        return finding
