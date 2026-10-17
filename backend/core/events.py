"""033：领域事件机制——简单的发布订阅（EventBus）。

事件：
- StoryboardUpdated：分镜被修改（镜头增删改、重排、重生成）。
- FactChanged：事实库条目状态/内容变更。
- ReviewInvalidated：审核结果失效（由订阅者自动触发，不直接发布）。

订阅者（033：分镜修改后自动）：
- on_storyboard_updated → 审核结果失效（expire_stale）+ 交付状态更新标记。

实现：
- 内存版 EventBus（同步分发，异常隔离：单个订阅者抛错不影响其他）。
- 留 Redis Stream 接口：EventPublisher 抽象，MemoryEventBus 为其内存实现；
  Phase 8 换 RedisStreamEventBus 即可，无需改业务代码。

使用示例：
    from backend.core.events import get_event_bus, StoryboardUpdated

    bus = get_event_bus()
    bus.subscribe(StoryboardUpdated, my_handler)
    bus.publish(StoryboardUpdated(work_id=..., storyboard_id=...,
                                  content_hash=..., actor_id=...))
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Type, TypeVar

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 事件定义

@dataclass(frozen=True)
class DomainEvent:
    """领域事件基类。"""
    occurred_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class StoryboardUpdated(DomainEvent):
    """分镜被修改。"""
    work_id: str = ""
    storyboard_id: str = ""
    # 修改后的内容哈希（version + 镜头快照），供审核失效比对
    content_hash: str = ""
    actor_id: str = ""
    # S6.3：发布方已在同一事务内完成审核失效时置 True，
    # 订阅者跳过 DB 写（避免 SQLite 下跨会话写锁冲突）。
    handled_inline: bool = False


@dataclass(frozen=True)
class FactChanged(DomainEvent):
    """事实库条目变更（状态流转/内容修改/过期）。"""
    org_id: str = ""
    fact_id: str = ""
    product_id: str = ""
    old_status: str = ""
    new_status: str = ""
    actor_id: str = ""
    # S6.1/S6.3：事实内容发生实质修改（状态未变）。已确认事实的内容变更
    # 同样使引用它的审核结论失效。
    content_changed: bool = False
    # S6.3：发布方已在同一事务内完成审核失效时置 True，
    # 订阅者跳过 DB 写（避免 SQLite 下跨会话写锁冲突）。
    handled_inline: bool = False


@dataclass(frozen=True)
class ReviewInvalidated(DomainEvent):
    """审核结果失效（由订阅者自动发布，供审计/通知）。"""
    work_id: str = ""
    review_id: str = ""
    reason: str = ""


E = TypeVar("E", bound=DomainEvent)
Handler = Callable[[E], None]


# ---------------------------------------------------------------- 发布者抽象

class EventPublisher:
    """事件发布者抽象——Phase 8 的 Redis Stream 实现替换这里即可。"""

    def publish(self, event: DomainEvent) -> None:
        raise NotImplementedError

    def subscribe(self, event_type: Type[E], handler: Handler[E]) -> None:
        raise NotImplementedError


class MemoryEventBus(EventPublisher):
    """033：内存版 EventBus（同步分发）。

    - 线程安全（订阅表加锁）。
    - 异常隔离：单个 handler 抛错只记日志，不阻断其他 handler。
    - 支持按事件类型精确订阅；基类 DomainEvent 订阅可收到所有事件。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[type, list[Handler]] = {}

    def subscribe(self, event_type: Type[E], handler: Handler[E]) -> None:
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: Type[E], handler: Handler[E]) -> None:
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    def publish(self, event: DomainEvent) -> None:
        # 收集：精确类型 + 基类订阅
        with self._lock:
            targets: list[Handler] = []
            for etype, handlers in self._handlers.items():
                if isinstance(event, etype):
                    targets.extend(handlers)
        for handler in targets:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 — 异常隔离
                logger.exception(
                    "事件处理器失败: %s event=%s",
                    getattr(handler, "__name__", handler), type(event).__name__)

    def clear(self) -> None:
        """测试用：清空订阅。"""
        with self._lock:
            self._handlers.clear()


# 全局单例（进程内）
_bus: MemoryEventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> MemoryEventBus:
    """取全局 EventBus 单例。"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = MemoryEventBus()
    return _bus


def reset_event_bus() -> None:
    """测试用：重置全局单例。"""
    global _bus
    with _bus_lock:
        _bus = None


# ---------------------------------------------------------------- 默认订阅者

def _default_subscribers_registered() -> bool:
    return getattr(get_event_bus(), "_lupin_default_subs", False)


def register_default_subscribers(bus: MemoryEventBus | None = None,
                                 db_session_factory: Any = None) -> None:
    """033：注册默认订阅者。

    - StoryboardUpdated → 审核结果失效（ReviewRepository.expire_stale）
      + 发布 ReviewInvalidated（供审计/通知）
    - FactChanged → S6.3：事实实质变更（confirmed → 不可用）时，
      该组织下所有"有效"审核自动失效（expire_all_for_org）

    db_session_factory：返回新 Session 的可调用对象；为 None 时订阅者
    只记日志不写库（测试/轻量场景）。
    """
    bus = bus or get_event_bus()
    if getattr(bus, "_lupin_default_subs", False):
        return

    def on_storyboard_updated(event: StoryboardUpdated) -> None:
        # S6.3：发布方已在同一事务内完成失效时，只记日志不碰 DB
        if event.handled_inline:
            logger.info("StoryboardUpdated（已内联处理）: %s",
                        event.storyboard_id)
            return
        if db_session_factory is None:
            logger.info("StoryboardUpdated（无 DB 会话，跳过失效）: %s",
                        event.storyboard_id)
            return
        try:
            from ..repositories.review_repository import ReviewRepository
            db = db_session_factory()
            try:
                repo = ReviewRepository(db)
                expired = repo.expire_stale(event.work_id, event.content_hash)
                if expired:
                    logger.info("分镜变更导致 %d 条审核失效 work=%s",
                                expired, event.work_id)
                    bus.publish(ReviewInvalidated(
                        work_id=event.work_id,
                        review_id="",
                        reason=f"分镜变更（hash={event.content_hash[:12]}），"
                               f"{expired} 条审核自动失效",
                    ))
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("StoryboardUpdated 订阅者处理失败")

    def on_fact_changed(event: FactChanged) -> None:
        # S6.3：事实发生实质变更（曾可用 confirmed → 不再可用）时，保守地将
        # 该组织下所有"有效"审核置为 expired（只做过期提示，不覆盖旧成果）。
        # 近似说明：精确到"引用了该事实的作品"需要事实→作品引用链，
        # 当前按组织级保守失效，Phase 6 后续可细化到 product 维度。
        material = (event.old_status == "confirmed"
                    and event.new_status != "confirmed")
        # S6.1：已确认事实的内容变更同样使审核失效
        content_material = (event.content_changed
                            and event.new_status == "confirmed")
        if not (material or content_material):
            logger.info("FactChanged（非实质变更，跳过失效）: fact=%s %s->%s",
                        event.fact_id, event.old_status, event.new_status)
            return
        # S6.3：发布方已在同一事务内完成失效时，只记日志不碰 DB
        if event.handled_inline:
            logger.info("FactChanged（已内联处理）: fact=%s", event.fact_id)
            return
        if db_session_factory is None:
            logger.info("FactChanged（无 DB 会话，跳过失效）: fact=%s",
                        event.fact_id)
            return
        try:
            from ..repositories.review_repository import ReviewRepository
            db = db_session_factory()
            try:
                repo = ReviewRepository(db)
                expired = repo.expire_all_for_org(event.org_id)
                if expired:
                    logger.info("事实变更导致 %d 条审核失效 org=%s fact=%s",
                                expired, event.org_id, event.fact_id)
                    bus.publish(ReviewInvalidated(
                        work_id="",
                        review_id="",
                        reason=f"事实变更（fact={event.fact_id[:12]}），"
                               f"{expired} 条审核自动失效",
                    ))
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("FactChanged 订阅者处理失败")

    bus.subscribe(StoryboardUpdated, on_storyboard_updated)
    bus.subscribe(FactChanged, on_fact_changed)
    bus._lupin_default_subs = True  # noqa: SLF001
