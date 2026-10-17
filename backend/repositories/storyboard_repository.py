"""分镜 / 镜头 Repository（084/087/088）。

- 087 示例：replace_shots() 把“删旧镜头 + 插新镜头 + 分镜 version+1”
  包在一个事务里，要么全成功要么全回滚。
- 088：分镜更新带 expected_version 乐观锁。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Shot, Storyboard
from ..core.events import (
    ReviewInvalidated,
    StoryboardUpdated,
    get_event_bus,
)
from .base import EntityNotFound, OptimisticLockError, transactional_method


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoryboardRepository:
    def __init__(self, db: Session):
        self.db = db

    def _emit_updated(self, storyboard_id: str) -> None:
        """S6.3：分镜写操作后发布 StoryboardUpdated 事件。

        审核失效在同一事务内直接完成（同一会话写 reviews，无跨会话
        写锁问题；主事务回滚时失效自动撤销，比跨会话更正确），事件
        标记 handled_inline=True 供订阅者跳过重复 DB 写。
        无订阅者时 publish 为 no-op。
        """
        sb = self.get(storyboard_id)
        if sb is None:
            return
        try:
            from ..services.review_service import compute_content_hash
            content_hash, _ = compute_content_hash(self.db, sb.work_id)
        except Exception:  # noqa: BLE001 — 哈希失败不阻断主写流程
            content_hash = ""
        expired = 0
        if content_hash:
            from .review_repository import ReviewRepository
            expired = ReviewRepository(self.db).expire_stale(
                sb.work_id, content_hash)
        bus = get_event_bus()
        bus.publish(StoryboardUpdated(
            work_id=sb.work_id, storyboard_id=storyboard_id,
            content_hash=content_hash, handled_inline=True))
        if expired:
            bus.publish(ReviewInvalidated(
                work_id=sb.work_id, review_id="",
                reason=f"分镜变更（hash={content_hash[:12]}），"
                       f"{expired} 条审核自动失效"))

    @transactional_method
    def create(self, work_id: str, title: str = "") -> Storyboard:
        sb = Storyboard(work_id=work_id, title=title.strip())
        self.db.add(sb)
        self.db.flush()
        self.db.refresh(sb)
        self._emit_updated(sb.id)
        return sb

    def get(self, storyboard_id: str) -> Storyboard | None:
        return self.db.get(Storyboard, storyboard_id)

    def list_by_work(self, work_id: str) -> list[Storyboard]:
        return list(self.db.scalars(
            select(Storyboard).where(Storyboard.work_id == work_id)
            .order_by(Storyboard.version.desc())))

    @transactional_method
    def update(self, storyboard_id: str,
               expected_version: int | None = None,
               title: str | None = None) -> Storyboard:
        values: dict[str, Any] = {"updated_at": _utcnow()}
        if title is not None:
            values["title"] = title.strip()
        if expected_version is not None:
            values["version"] = Storyboard.version + 1
            result = self.db.execute(
                update(Storyboard)
                .where(Storyboard.id == storyboard_id,
                       Storyboard.version == expected_version)
                .values(**values))
            if result.rowcount == 0:
                sb = self.get(storyboard_id)
                if sb is None:
                    raise EntityNotFound("Storyboard", storyboard_id)
                raise OptimisticLockError("Storyboard", storyboard_id,
                                          expected_version, sb.version)
        else:
            result = self.db.execute(
                update(Storyboard).where(Storyboard.id == storyboard_id)
                .values(**values))
            if result.rowcount == 0:
                raise EntityNotFound("Storyboard", storyboard_id)
        self.db.flush()
        sb = self.get(storyboard_id)
        assert sb is not None
        self._emit_updated(storyboard_id)
        return sb

    # ---- 镜头 ----

    @transactional_method
    def add_shot(self, storyboard_id: str, shot_no: int,
                 content: dict[str, Any] | None = None,
                 duration_seconds: int = 0,
                 shot_id_stable: str | None = None) -> Shot:
        if self.get(storyboard_id) is None:
            raise EntityNotFound("Storyboard", storyboard_id)
        shot = Shot(
            storyboard_id=storyboard_id, shot_no=shot_no,
            content_json=content or {}, duration_seconds=duration_seconds,
            shot_id_stable=shot_id_stable,
        )
        self.db.add(shot)
        self.db.flush()
        self.db.refresh(shot)
        self._emit_updated(storyboard_id)
        return shot

    def list_shots(self, storyboard_id: str) -> list[Shot]:
        return list(self.db.scalars(
            select(Shot).where(Shot.storyboard_id == storyboard_id)
            .order_by(Shot.shot_no.asc())))

    def get_shot_by_stable_id(self, storyboard_id: str,
                              shot_id_stable: str) -> Shot | None:
        """084：按稳定 ID 取镜头——重排序后仍能关联拍摄记录/素材/审核。"""
        return self.db.scalar(
            select(Shot).where(
                Shot.storyboard_id == storyboard_id,
                Shot.shot_id_stable == shot_id_stable))

    @transactional_method
    def replace_shots(self, storyboard_id: str, expected_version: int,
                      shots: list[dict[str, Any]]) -> Storyboard:
        """087：删旧镜头 + 插新镜头 + 分镜 version+1，原子提交。

        shots 每项：{shot_no, content, duration_seconds, shot_id_stable?}
        """
        sb = self.get(storyboard_id)
        if sb is None:
            raise EntityNotFound("Storyboard", storyboard_id)
        if sb.version != expected_version:
            raise OptimisticLockError("Storyboard", storyboard_id,
                                      expected_version, sb.version)
        # 删旧（CASCADE 也会处理，但显式删更清晰）
        for old in self.list_shots(storyboard_id):
            self.db.delete(old)
        self.db.flush()
        for s in shots:
            self.db.add(Shot(
                storyboard_id=storyboard_id,
                shot_no=int(s.get("shot_no", 1)),
                content_json=s.get("content") or {},
                duration_seconds=int(s.get("duration_seconds", 0)),
                shot_id_stable=s.get("shot_id_stable"),
            ))
        sb.version = sb.version + 1
        sb.updated_at = _utcnow()
        self.db.flush()
        self.db.refresh(sb)
        self._emit_updated(storyboard_id)
        return sb

    @transactional_method
    def reorder_shots(self, storyboard_id: str,
                      ordered_shot_ids: list[str]) -> None:
        """087：按稳定 ID 列表重排 shot_no，原子提交。"""
        shots = {s.id: s for s in self.list_shots(storyboard_id)}
        if set(ordered_shot_ids) != set(shots.keys()):
            raise ValueError("重排列表与现有镜头集合不一致")
        for idx, sid in enumerate(ordered_shot_ids, start=1):
            shots[sid].shot_no = idx
        self.db.flush()
        self._emit_updated(storyboard_id)

    # ---- 032：镜头锁定状态（事务边界） ----

    @transactional_method
    def set_shot_lock(self, storyboard_id: str, shot_id: str,
                      locked: bool) -> None:
        """032：单个镜头锁定/解锁——事务内修改，失败回滚。

        锁定镜头在 AI 重生成时受保护（app.py 的 locked 列表即来源于此）。
        """
        shot = self.db.get(Shot, shot_id)
        if shot is None or shot.storyboard_id != storyboard_id:
            raise EntityNotFound("Shot", shot_id)
        shot.locked = locked
        self.db.flush()
        self._emit_updated(storyboard_id)

    @transactional_method
    def set_shots_lock(self, storyboard_id: str,
                       shot_ids: list[str], locked: bool) -> int:
        """032：批量锁定/解锁——同一事务内完成，要么全成功要么全回滚。

        任一 shot_id 非法则抛 EntityNotFound，整个批次回滚。
        返回实际修改的条数。
        """
        count = 0
        for sid in shot_ids:
            shot = self.db.get(Shot, sid)
            if shot is None or shot.storyboard_id != storyboard_id:
                raise EntityNotFound("Shot", sid)
            shot.locked = locked
            count += 1
        self.db.flush()
        self._emit_updated(storyboard_id)
        return count

    def list_locked_shot_nos(self, storyboard_id: str) -> list[int]:
        """取已锁定镜头的展示编号（供 AI 重生成时保护）。"""
        return list(self.db.scalars(
            select(Shot.shot_no).where(
                Shot.storyboard_id == storyboard_id,
                Shot.locked.is_(True))
            .order_by(Shot.shot_no.asc())))
