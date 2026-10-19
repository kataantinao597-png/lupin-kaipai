"""事实库 Repository（085/087/088/S6.1）。

- list_usable() 只返回 status=confirmed 且 allow_use=true 的事实（010/118），
  供 AI 生成时引用。
- set_status() 只能由人工/确定性规则调用，模型无权自升状态（143）。
- S6.1：transition() 为唯一推荐的状态流转入口——经状态机校验、
  写审计、发 FactChanged 事件；update_content() 做内容版本快照。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..facts import state_machine as sm
from ..models import FACT_STATUSES, Fact, FactStatusHistory, FactVersion
from ..core.events import FactChanged, ReviewInvalidated, get_event_bus
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# S6.1：事实内容版本快照上限（与 work_versions 的 50 保持一致）
MAX_VERSIONS_PER_FACT = 50


class FactTransitionError(ValueError):
    """transition() 失败：非法流转或规则校验不通过。"""


class FactRepository:
    def __init__(self, db: Session):
        self.db = db

    def _expire_reviews_inline(self, org_id: str, fact_id: str) -> None:
        """S6.3：事实实质变更时，在同一事务内失效该组织的有效审核。

        同一会话写 reviews，无跨会话写锁问题；主事务回滚时失效自动撤销。
        """
        from .review_repository import ReviewRepository
        expired = ReviewRepository(self.db).expire_all_for_org(org_id)
        if expired:
            get_event_bus().publish(ReviewInvalidated(
                work_id="", review_id="",
                reason=f"事实变更（fact={fact_id[:12]}），"
                       f"{expired} 条审核自动失效"))

    @staticmethod
    def _is_material_change(old_status: str, new_status: str) -> bool:
        """S6.3：实质变更 = 曾可用（confirmed）→ 不再可用。"""
        return old_status == "confirmed" and new_status != "confirmed"

    @transactional_method
    def create(self, org_id: str, content: str, product_id: str = "",
               category: str = "", source_type: str = "human_statement",
               source_ref: str = "", scope: str = "",
               expires_at: datetime | None = None) -> Fact:
        fact = Fact(
            org_id=org_id, content=content.strip(), product_id=product_id,
            category=category, source_type=source_type, source_ref=source_ref,
            scope=scope or "",
            expires_at=expires_at,
        )
        self.db.add(fact)
        self.db.flush()
        self.db.refresh(fact)
        # S6.3：新事实进入待审核池也通知（订阅者只对实质变更做失效）
        get_event_bus().publish(FactChanged(
            org_id=org_id, fact_id=fact.id, product_id=product_id,
            old_status="", new_status=fact.status or "pending",
            handled_inline=True))
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
                    product_id: str | None = None,
                    limit: int = 200, offset: int = 0) -> list[Fact]:
        stmt = select(Fact).where(Fact.org_id == org_id)
        if status is not None:
            # S6.1：兼容历史 "pending" 查询参数
            status = sm.normalize_status(status)
            if status not in FACT_STATUSES:
                raise ValueError(f"非法事实状态: {status}")
            stmt = stmt.where(Fact.status == status)
        if product_id is not None:
            # V3.0 B-2：按产品筛选。product_id 是自由字符串
            # （可引用产品 id 或 code，兼容存量数据）。
            stmt = stmt.where(Fact.product_id == product_id)
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
        # S6.3：记录变更前状态，供 FactChanged 事件判断是否为实质变更
        _row = self.db.execute(
            select(Fact.status, Fact.product_id).where(
                Fact.id == fact_id, Fact.org_id == org_id)).first()
        old_status = _row[0] if _row else ""
        product_id = _row[1] if _row else ""
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
        # S6.3：事实状态变更通知；实质变更时同一事务内失效组织有效审核
        get_event_bus().publish(FactChanged(
            org_id=org_id, fact_id=fact_id, product_id=product_id or "",
            old_status=old_status, new_status=status,
            handled_inline=True))
        if self._is_material_change(old_status, status):
            self._expire_reviews_inline(org_id, fact_id)
        return fact

    def count_by_org(self, org_id: str) -> int:
        return self.db.scalar(
            select(func.count()).select_from(Fact).where(
                Fact.org_id == org_id)) or 0

    # ---------------- S6.1：状态机流转 ----------------

    @transactional_method
    def transition(self, fact_id: str, org_id: str, target_status: str,
                   operator_id: str, reason: str = "",
                   evidence_ref: str = "", expected_version: int | None = None,
                   allow_use: bool | None = None) -> Fact:
        """S6.1：事实状态流转（唯一推荐入口）。

        - 经状态机校验合法性，非法抛 FactTransitionError
        - confirmed/rejected/revoked 必须填 reason，否则抛错
        - confirmed 要求 allow_use=true 且 verified_by=operator_id
        - 写 fact_status_history 审计 + 发 FactChanged 事件
        - expected_version 传入时做乐观锁；None 则跳过版本校验
        """
        fact = self.get(fact_id, org_id)
        if fact is None:
            raise EntityNotFound("Fact", fact_id)
        try:
            frm, to = sm.validate_transition(fact.status, target_status)
        except (sm.IllegalFactTransition,
                sm.FactTransitionRuleError) as e:
            raise FactTransitionError(str(e)) from e
        if sm.requires_reason(to) and not (reason or "").strip():
            raise FactTransitionError(
                f"流转到 {to} 必须填写原因（reason）")

        values: dict[str, Any] = {
            "status": to,
            "version": Fact.version + 1,
            "updated_at": _utcnow(),
        }
        if to == sm.CONFIRMED:
            # S6.1：确认的硬性条件；allow_use 默认 true（确认即允许作为宣传依据），
            # 显式传 False 则走校验拒绝路径
            use = True if allow_use is None else allow_use
            try:
                sm.check_confirm_requirements(use, operator_id)
            except sm.FactTransitionRuleError as e:
                raise FactTransitionError(str(e)) from e
            values["allow_use"] = use
            values["verified_by"] = operator_id
            values["verified_at"] = _utcnow()
        elif allow_use is not None:
            values["allow_use"] = allow_use

        where = [Fact.id == fact_id, Fact.org_id == org_id]
        if expected_version is not None:
            where.append(Fact.version == expected_version)
        result = self.db.execute(update(Fact).where(*where).values(**values))
        if result.rowcount == 0:
            # 088：要么并发冲突，要么事实已被删/转移组织
            cur = self.get(fact_id, org_id)
            if cur is None:
                raise EntityNotFound("Fact", fact_id)
            raise OptimisticLockError("Fact", fact_id,
                                      expected_version, cur.version)
        # S6.1：审计记录（与状态变更同一事务）
        self.db.add(FactStatusHistory(
            fact_id=fact_id, org_id=org_id,
            from_status=frm, to_status=to,
            operator_id=operator_id,
            reason=(reason or "").strip(),
            evidence_ref=(evidence_ref or "").strip(),
        ))
        self.db.flush()
        fact = self.get(fact_id, org_id)
        assert fact is not None
        self._publish_fact_changed(org_id, fact, frm, to, operator_id)
        # S6.3：实质变更时同一事务内失效组织有效审核
        if self._is_material_change(frm, to):
            self._expire_reviews_inline(org_id, fact_id)
        return fact

    def _publish_fact_changed(self, org_id: str, fact: Fact,
                              old_status: str, new_status: str,
                              actor_id: str,
                              content_changed: bool = False) -> None:
        """发 FactChanged 事件（033 事件总线；订阅者异常隔离，失败不阻断主流程）。

        S6.3：调用方已在同一事务内完成审核失效，事件标记 handled_inline。
        """
        try:
            from ..core.events import FactChanged, get_event_bus
            get_event_bus().publish(FactChanged(
                org_id=org_id, fact_id=fact.id,
                product_id=fact.product_id or "",
                old_status=old_status, new_status=new_status,
                actor_id=actor_id or "",
                content_changed=content_changed,
                handled_inline=True,
            ))
        except Exception:  # noqa: BLE001 — 事件失败不阻断业务
            import logging
            logging.getLogger(__name__).exception("发布 FactChanged 事件失败")

    # ---------------- S6.1：内容版本 ----------------

    @transactional_method
    def update_content(self, fact_id: str, org_id: str,
                       operator_id: str, expected_version: int,
                       content: str | None = None,
                       category: str | None = None,
                       source_type: str | None = None,
                       source_ref: str | None = None) -> Fact:
        """S6.1：更新事实内容。变更前先存快照到 fact_versions，version+1。

        任一内容字段变化即视为新版本；无变化直接返回原事实。
        """
        fact = self.get(fact_id, org_id)
        if fact is None:
            raise EntityNotFound("Fact", fact_id)
        if fact.version != expected_version:
            raise OptimisticLockError("Fact", fact_id,
                                      expected_version, fact.version)
        new_vals: dict[str, Any] = {}
        if content is not None and content.strip() != (fact.content or ""):
            new_vals["content"] = content.strip()
        if category is not None and category != (fact.category or ""):
            new_vals["category"] = category
        if source_type is not None and source_type != (fact.source_type or ""):
            new_vals["source_type"] = source_type
        if source_ref is not None and source_ref != (fact.source_ref or ""):
            new_vals["source_ref"] = source_ref
        if not new_vals:
            return fact  # 无实质变化

        # 存快照（旧内容）
        last = (self.db.query(FactVersion)
                .filter(FactVersion.fact_id == fact_id)
                .order_by(FactVersion.version_no.desc())
                .first())
        next_no = (last.version_no + 1) if last else 1
        self.db.add(FactVersion(
            fact_id=fact_id, version_no=next_no,
            content=fact.content or "", category=fact.category or "",
            source_type=fact.source_type or "",
            source_ref=fact.source_ref or "",
            status=sm.normalize_status(fact.status),
            allow_use=bool(fact.allow_use),
            created_by=operator_id,
        ))
        # 超限删最旧
        excess = (self.db.query(FactVersion)
                  .filter(FactVersion.fact_id == fact_id)
                  .order_by(FactVersion.version_no.asc())
                  .offset(MAX_VERSIONS_PER_FACT)
                  .all())
        for old in excess:
            self.db.delete(old)

        new_vals["version"] = Fact.version + 1
        new_vals["updated_at"] = _utcnow()
        self.db.execute(
            update(Fact)
            .where(Fact.id == fact_id, Fact.org_id == org_id)
            .values(**new_vals))
        self.db.flush()
        fact = self.get(fact_id, org_id)
        assert fact is not None
        # 内容变更也发事件（订阅者可据此让相关审核失效）
        self._publish_fact_changed(org_id, fact, sm.normalize_status(fact.status),
                                   sm.normalize_status(fact.status), operator_id,
                                   content_changed=True)
        # S6.3：已确认事实的内容变更，同一事务内失效组织有效审核
        if sm.normalize_status(fact.status) == "confirmed":
            self._expire_reviews_inline(org_id, fact_id)
        return fact

    # ---------------- S6.1：过期扫库 ----------------

    @transactional_method
    def expire_due(self, org_id: str | None = None) -> int:
        """S6.1：将已过有效期的 confirmed 事实自动转 expired。

        采用"定时扫库"策略：由 scripts/expire_facts.py 挂 cron 调用；
        查询侧 list_usable() 另有懒过滤（expires_at > now）做双保险。
        返回本次过期的条数。operator 记为 "system"。
        """
        now = _utcnow()
        stmt = select(Fact).where(
            Fact.status == sm.CONFIRMED,
            Fact.expires_at.is_not(None),
            Fact.expires_at <= now,
        )
        if org_id:
            stmt = stmt.where(Fact.org_id == org_id)
        due = list(self.db.scalars(stmt))
        for fact in due:
            self.db.execute(
                update(Fact)
                .where(Fact.id == fact.id)
                .values(status=sm.EXPIRED, version=Fact.version + 1,
                        updated_at=now))
            self.db.add(FactStatusHistory(
                fact_id=fact.id, org_id=fact.org_id,
                from_status=sm.CONFIRMED, to_status=sm.EXPIRED,
                operator_id="system",
                reason="有效期到期，系统自动过期",
                evidence_ref="",
            ))
            self.db.flush()
            self._publish_fact_changed(fact.org_id, fact,
                                       sm.CONFIRMED, sm.EXPIRED, "system")
            # S6.3：到期自动过期是实质变更，同一事务内失效组织有效审核
            self._expire_reviews_inline(fact.org_id, fact.id)
        return len(due)

    # ---------------- S6.1：审计查询 ----------------

    def list_history(self, fact_id: str, org_id: str,
                     limit: int = 100) -> list[FactStatusHistory]:
        """取某事实的状态变更审计记录（时间倒序）。"""
        return list(self.db.scalars(
            select(FactStatusHistory)
            .where(FactStatusHistory.fact_id == fact_id,
                   FactStatusHistory.org_id == org_id)
            .order_by(FactStatusHistory.created_at.desc())
            .limit(limit)))

    def list_versions(self, fact_id: str, org_id: str,
                      limit: int = 50) -> list[FactVersion]:
        """取某事实的内容版本快照（version_no 倒序）。"""
        fact = self.get(fact_id, org_id)
        if fact is None:
            raise EntityNotFound("Fact", fact_id)
        return list(self.db.scalars(
            select(FactVersion)
            .where(FactVersion.fact_id == fact_id)
            .order_by(FactVersion.version_no.desc())
            .limit(limit)))
