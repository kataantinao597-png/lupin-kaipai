"""035：后台任务队列——ThreadPoolExecutor + SQLite（ai_tasks 表）。

设计决策（035）：
- 不用 Celery/Redis：部署简单、零外部依赖；Phase 8 流量上来再升级。
- 每个任务独立 DB 会话（线程安全）；执行器全局单例。
- 038 取消语义：
  - pending（还没被工作线程取走）：直接置 cancelled。
  - running：置 cancel_requested=True，工作线程协作式检查
    （AI 调用开始前 + 完成后各检查一次；调用中无法中断，
    完成后按 cancelled 落库、不写 result）。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 035：工作线程数；AI 调用是 IO 密集型，4 线程够 MVP 用
MAX_WORKERS = 4

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
# task_id -> Future（用于判断 pending/running）
_futures: dict[str, Any] = {}
_futures_lock = threading.Lock()


def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS,
                    thread_name_prefix="lupin-ai-task",
                )
    return _executor


def _remember_future(task_id: str, future: Any) -> None:
    with _futures_lock:
        _futures[task_id] = future


def _forget_future(task_id: str) -> None:
    with _futures_lock:
        _futures.pop(task_id, None)


def _future_for(task_id: str) -> Any | None:
    with _futures_lock:
        return _futures.get(task_id)


class TaskQueue:
    """035：任务队列门面。"""

    def __init__(self, session_factory: Callable[[], Any]):
        """
        session_factory：返回新 SQLAlchemy Session 的可调用对象
        （如 backend.auth.database.get_session_factory）。
        """
        self._session_factory = session_factory

    def _new_repo(self):
        from .repository import AITaskRepository
        return AITaskRepository(self._session_factory())

    def submit(self, task_id: str,
               runner: Callable[[str, Callable[[], bool]], dict[str, Any]],
               model_used: str | None = None) -> None:
        """提交任务到线程池。

        runner(task_id, is_cancelled) -> result_dict：
        - is_cancelled() 由 runner 在关键点调用（协作式取消）。
        - 返回结果 dict，成功时落库为 result_json。
        """
        from .repository import AITaskRepository

        def _run():
            db = self._session_factory()
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
                    db2 = self._session_factory()
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
            except Exception as e:  # noqa: BLE001
                logger.exception("AI 任务执行失败 task=%s", task_id)
                try:
                    # 失败前最后检查取消标记，取消优先
                    db3 = self._session_factory()
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
                _forget_future(task_id)

        future = get_executor().submit(_run)
        _remember_future(task_id, future)

    def cancel(self, task_id: str) -> str:
        """038：取消任务。返回最终状态。

        - pending（Future 还没开始跑）：尝试 future.cancel()，
          成功则直接置 cancelled。
        - running / cancel 不掉：打 cancel_requested 标记，协作式取消。
        """
        from .repository import AITaskRepository

        db = self._session_factory()
        try:
            repo = AITaskRepository(db)
            task = repo.get_or_404(task_id)
            if task.status in ("succeeded", "failed", "cancelled"):
                return task.status  # 幂等
            future = _future_for(task_id)
            if task.status == "pending" and future is not None:
                if future.cancel():
                    # 取消排队成功
                    repo.set_status(task_id, "cancelled",
                                    error="任务被取消（排队中）")
                    _forget_future(task_id)
                    return "cancelled"
            # running 或 cancel 不掉 → 协作式标记
            repo.request_cancel(task_id)
            return task.status
        finally:
            db.close()


class _TaskCancelled(Exception):
    """runner 内部用：检测到取消时抛此异常，queue 转为 cancelled 落库。"""
