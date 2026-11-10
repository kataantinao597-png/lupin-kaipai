"""O-04：超时扫描后台线程（CASE-20261109-005）。

此前 ``sweep_timed_out`` 由两个读接口在轮询时惰性触发
（``GET /ai-tasks/{id}``、``GET /agent/runs/{id}``），每轮询一次
就全表扫一次。现改为应用启动的常驻后台线程，每 60s 跑一次，
轮询接口只读状态。

照搬项目已有先例：``backend/media/worker.py``（守护线程 +
``_stop_event`` + 每轮 try/except，线程永不崩）。

单 worker 假设：两份 Dockerfile 均 ``--workers 1``，单进程内只会
起一个扫描线程；即使将来多 worker，``sweep_timed_out`` 本身是
幂等设计（终态竞态逐任务跳过），重复扫也只是多做无用功，不会
把任务状态写坏。

异常自保（线程死掉不能静默）：
- 每轮 sweep 的异常当轮捕获并记 error 级日志，线程继续跑；
- 连续失败次数与最近一次错误记在 ``sweeper_status()`` 里，可查；
- 线程体本身再包一层兜底 try/except，万一循环意外退出记 critical。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .queue import sweep_timed_out

log = logging.getLogger(__name__)

#: 扫描周期（秒）：超时翻转最长延迟一个周期。
SWEEP_INTERVAL_SECONDS = 60.0

_sweeper_thread: threading.Thread | None = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "started_at": None,       # 线程启动时刻（ISO 字符串）
    "last_run_at": None,      # 最近一次 sweep 开始时刻
    "last_swept": None,       # 最近一次 sweep 处理的任务数
    "last_error": None,       # 最近一次 sweep 的错误（字符串，无则 None）
    "consecutive_failures": 0,
}


def _record_run(n: int) -> None:
    with _status_lock:
        _status["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _status["last_swept"] = n
        _status["last_error"] = None
        _status["consecutive_failures"] = 0


def _record_failure(err: BaseException) -> None:
    with _status_lock:
        _status["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _status["last_error"] = f"{type(err).__name__}: {err}"
        _status["consecutive_failures"] += 1


def _loop(interval: float) -> None:
    log.info("超时扫描线程已启动（每 %ss）", interval)
    try:
        while not _stop_event.is_set():
            try:
                # 函数内导入：session_factory 每次现取，避免模块导入
                # 顺序问题；每个 sweep 用独立短会话（与 queue 注释一致）。
                from ..auth.database import get_session_factory

                n = sweep_timed_out(get_session_factory())
                _record_run(n)
                if n:
                    log.info("超时扫描标记 %d 个任务", n)
            except Exception as e:  # noqa: BLE001 — 单轮失败不杀线程
                _record_failure(e)
                log.error("超时扫描失败（线程继续运行）: %s",
                          _status["last_error"], exc_info=True)
            _stop_event.wait(interval)
    except Exception:  # noqa: BLE001 — 循环本身意外退出绝不能静默
        log.critical("超时扫描线程意外退出", exc_info=True)
    log.info("超时扫描线程已停止")


def start_timeout_sweeper(interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    """启动超时扫描守护线程（幂等，已在跑则不动）。"""
    global _sweeper_thread
    if _sweeper_thread is not None and _sweeper_thread.is_alive():
        return
    _stop_event.clear()
    with _status_lock:
        _status["started_at"] = datetime.now(timezone.utc).isoformat()
        _status["interval_seconds"] = interval
    _sweeper_thread = threading.Thread(
        target=_loop, args=(interval,), name="lupin-timeout-sweeper",
        daemon=True)
    _sweeper_thread.start()


def stop_timeout_sweeper(timeout: float = 5.0) -> None:
    """停止扫描线程（测试/优雅退出用）。"""
    _stop_event.set()
    if _sweeper_thread is not None:
        _sweeper_thread.join(timeout=timeout)


def sweeper_status() -> dict[str, Any]:
    """扫描线程状态（排查用）：是否存活、最近一次跑了什么、有无连续失败。"""
    with _status_lock:
        out = dict(_status)
    out["alive"] = bool(_sweeper_thread is not None
                        and _sweeper_thread.is_alive())
    out.setdefault("interval_seconds", SWEEP_INTERVAL_SECONDS)
    return out
