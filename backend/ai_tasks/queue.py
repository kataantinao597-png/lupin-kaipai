"""035：后台任务队列——优先级调度 + 工作线程 + SQLite（ai_tasks 表）。

设计决策（035）：
- 不用 Celery/Redis：部署简单、零外部依赖；Phase 8 流量上来再升级。
- 每个任务独立 DB 会话（线程安全）；调度器全局单例。
- 137：优先级调度——high（用户交互）任务优先于 low（后台批量）执行。
  同优先级内按提交顺序 FIFO。
- 038 取消语义：
  - pending（还在排队）：直接置 cancelled，不再启动。
  - running：置 cancel_requested=True，工作线程协作式检查
    （AI 调用开始前 + 完成后各检查一次；调用中无法中断，
    完成后按 cancelled 落库、不写 result）。
"""
from __future__ import annotations

import itertools
import logging
import queue as _queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 035：工作线程数；AI 调用是 IO 密集型，4 线程够 MVP 用
MAX_WORKERS = 4

# 137：优先级权重（越大越先执行）
_PRIORITY_WEIGHT = {"high": 1, "low": 0}
DEFAULT_PRIORITY = "high"


class _PriorityDispatcher:
    """全局单例调度器：PriorityQueue + 固定工作线程。"""

    def __init__(self, max_workers: int = MAX_WORKERS):
        self._pq: "_queue.PriorityQueue[tuple]" = _queue.PriorityQueue()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._jobs: dict[str, tuple] = {}   # task_id -> (runner, model_used, session_factory)
        self._pending: set[str] = set()     # 已提交、尚未被工作线程取走
        self._running: set[str] = set()     # 正在执行
        self._workers = [
            threading.Thread(target=self._worker_loop,
                             name=f"lupin-ai-task-{i}", daemon=True)
            for i in range(max_workers)
        ]
        for t in self._workers:
            t.start()

    def submit(self, task_id: str,
               runner: Callable[[str, Callable[[], bool]], dict[str, Any]],
               model_used: str | None,
               session_factory: Callable[[], Any],
               priority: str = DEFAULT_PRIORITY) -> None:
        weight = _PRIORITY_WEIGHT.get(priority, _PRIORITY_WEIGHT[DEFAULT_PRIORITY])
        entry = (-weight, next(self._seq), task_id)
        with self._lock:
            self._jobs[task_id] = (runner, model_used, session_factory)
            self._pending.add(task_id)
        self._pq.put(entry)

    def is_pending(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._pending

    def _worker_loop(self) -> None:
        while True:
            _neg_w, _seq, task_id = self._pq.get()
            with self._lock:
                if task_id not in self._pending:
                    # 排队期间被取消了：跳过
                    self._jobs.pop(task_id, None)
                    continue
                self._pending.discard(task_id)
                self._running.add(task_id)
                job = self._jobs.get(task_id)
            if job is None:
                with self._lock:
                    self._running.discard(task_id)
                continue
            runner, model_used, session_factory = job
            try:
                self._execute(task_id, runner, model_used, session_factory)
            finally:
                with self._lock:
                    self._running.discard(task_id)
                    self._jobs.pop(task_id, None)

    def _execute(self, task_id: str,
                 runner: Callable[[str, Callable[[], bool]], dict[str, Any]],
                 model_used: str | None,
                 session_factory: Callable[[], Any]) -> None:
        from .repository import AITaskRepository

        db = session_factory()
        try:
            repo = AITaskRepository(db)
            # 取走前再确认一次：可能在排队时被取消了
            task = repo.get(task_id)
            if task is None:
                return
            if task.status == "cancelled" or task.cancel_requested:
                # 已取消的不再启动
                if task.status != "cancelled":
                    repo.set_status(task_id, "cancelled",
                                    error="排队期间被取消")
                return
            repo.set_status(task_id, "running", model_used=model_used)

            def is_cancelled() -> bool:
                # 独立短查询，避免长会话缓存
                db2 = session_factory()
                try:
                    return AITaskRepository(db2).is_cancel_requested(task_id)
                finally:
                    db2.close()

            try:
                result = runner(task_id, is_cancelled)
            except _TaskCancelled:
                repo.set_status(task_id, "cancelled", error="任务被取消")
                return
            # 完成后复查：跑的过程中被取消 → 按取消落库
            if is_cancelled():
                repo.set_status(task_id, "cancelled", error="任务被取消")
                return
            repo.set_status(task_id, "succeeded", result=result,
                            progress=100)
            # S10：任务完成事件（best-effort，不影响主流程）
            try:
                from ..services.webhook_service import dispatch_event
                dispatch_event("task.completed", task.org_id, {
                    "task_id": task_id, "task_type": task.task_type,
                    "status": "succeeded"})
            except Exception:  # noqa: BLE001
                logger.warning("task.completed webhook 投递失败 task=%s",
                               task_id, exc_info=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("AI 任务执行失败 task=%s", task_id)
            try:
                # 失败前最后检查取消标记，取消优先
                db3 = session_factory()
                try:
                    cancelled = AITaskRepository(db3).is_cancel_requested(
                        task_id)
                finally:
                    db3.close()
                if cancelled:
                    repo.set_status(task_id, "cancelled",
                                    error="任务被取消")
                else:
                    repo.set_status(
                        task_id, "failed",
                        error=f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                logger.exception("任务失败落库也失败 task=%s", task_id)
        finally:
            db.close()


_dispatcher: _PriorityDispatcher | None = None
_dispatcher_lock = threading.Lock()


def get_dispatcher() -> _PriorityDispatcher:
    """全局调度器单例（延迟初始化）。"""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = _PriorityDispatcher()
    return _dispatcher


def get_executor():
    """兼容旧名：返回全局优先级调度器（替代 ThreadPoolExecutor）。"""
    return get_dispatcher()


class TaskQueue:
    """035：任务队列门面。137：submit 支持 priority。"""

    def __init__(self, session_factory: Callable[[], Any]):
        """
        session_factory：返回新 SQLAlchemy Session 的可调用对象
        （如 backend.auth.database.get_session_factory）。
        """
        self._session_factory = session_factory

    def submit(self, task_id: str,
               runner: Callable[[str, Callable[[], bool]], dict[str, Any]],
               model_used: str | None = None,
               priority: str = DEFAULT_PRIORITY) -> None:
        """提交任务到优先级队列。

        runner(task_id, is_cancelled) -> result_dict：
        - is_cancelled() 由 runner 在关键点调用（协作式取消）。
        - 返回结果 dict，成功时落库为 result_json。
        - priority: "high"（用户交互）优先于 "low"（后台批量）执行。
        """
        get_dispatcher().submit(task_id, runner, model_used,
                                self._session_factory, priority=priority)

    def cancel(self, task_id: str) -> str:
        """038：取消任务。返回最终状态。

        - pending（还在排队）：直接置 cancelled，不再启动。
        - running：打 cancel_requested 标记，协作式取消。
        """
        from .repository import AITaskRepository

        db = self._session_factory()
        try:
            repo = AITaskRepository(db)
            task = repo.get_or_404(task_id)
            if task.status in ("succeeded", "failed", "cancelled"):
                return task.status  # 幂等
            dispatcher = get_dispatcher()
            if task.status == "pending" and dispatcher.is_pending(task_id):
                # 排队中：从待执行集合移除，工作线程取到时会跳过
                with dispatcher._lock:
                    dispatcher._pending.discard(task_id)
                    dispatcher._jobs.pop(task_id, None)
                repo.set_status(task_id, "cancelled",
                                error="任务被取消（排队中）")
                return "cancelled"
            # running → 协作式标记
            repo.request_cancel(task_id)
            return task.status
        finally:
            db.close()


class _TaskCancelled(Exception):
    """runner 内部用：检测到取消时抛此异常，queue 转为 cancelled 落库。"""
