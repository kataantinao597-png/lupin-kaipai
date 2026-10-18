"""S8.2：媒体处理工作线程。

- 单守护线程，轮询 media_tasks.pending（2s 间隔）
- 每个任务独立 DB 会话；状态机 pending→running→succeeded/failed
- 提取元数据后写回 media_tasks.meta_json，并同步到 assets.meta_json
- attempts 计数；单次失败即 failed（元数据提取是幂等的，失败可手动重跑）

启动：在 backend/app.py 的 startup 事件中调 start_media_worker()
（try/except 包裹，失败不阻断启动）。
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("lupin.media.worker")

_POLL_INTERVAL = 2.0
_MAX_ATTEMPTS = 3

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _process_one() -> bool:
    """取一条 pending 任务处理。返回 True=处理了一条。"""
    from ..auth.database import get_session_factory
    from .. import models
    from .probe import extract_metadata
    from .storage import get_storage

    session_factory = get_session_factory()
    db = session_factory()
    try:
        task = (db.query(models.MediaTask)
                .filter(models.MediaTask.status == models.MEDIA_TASK_PENDING)
                .order_by(models.MediaTask.created_at.asc())
                .with_for_update(skip_locked=True)
                .first())
        if task is None:
            return False
        task.status = models.MEDIA_TASK_RUNNING
        task.attempts = (task.attempts or 0) + 1
        db.commit()

        asset = db.get(models.Asset, task.asset_id)
        if asset is None or asset.status == models.ASSET_STATUS_DELETED:
            task.status = models.MEDIA_TASK_FAILED
            task.error = "资产不存在或已删除"
            db.commit()
            return True

        try:
            storage = get_storage()
            data = storage.get(asset.key)
            local_path = storage.local_path(asset.key)
            meta = extract_metadata(data, asset.mime or "", local_path)
            meta_json = json.dumps(meta, ensure_ascii=False)
            task.meta_json = meta_json
            task.status = models.MEDIA_TASK_SUCCEEDED
            task.error = None
            asset.meta_json = meta_json
            db.commit()
            log.info(f"媒体任务完成 asset={asset.id} meta={meta_json[:120]}")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            task = db.get(models.MediaTask, task.id)
            if task.attempts >= _MAX_ATTEMPTS:
                task.status = models.MEDIA_TASK_FAILED
                task.error = f"{type(e).__name__}: {e}"[:500]
            else:
                task.status = models.MEDIA_TASK_PENDING  # 回队重试
                task.error = f"第{task.attempts}次失败，稍后重试: {e}"[:500]
            db.commit()
            log.warning(f"媒体任务失败 asset={asset.id}: {e}")
        return True
    finally:
        db.close()


def _loop() -> None:
    log.info("媒体处理线程已启动")
    while not _stop_event.is_set():
        try:
            if not _process_one():
                _stop_event.wait(_POLL_INTERVAL)
        except Exception as e:  # noqa: BLE001 — 线程永不崩
            log.error(f"媒体处理轮询异常: {type(e).__name__}: {e}")
            _stop_event.wait(_POLL_INTERVAL)
    log.info("媒体处理线程已停止")


def start_media_worker() -> None:
    """启动守护线程（幂等）。"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_loop, name="lupin-media-worker",
                                      daemon=True)
    _worker_thread.start()


def stop_media_worker(timeout: float = 5.0) -> None:
    """停止（测试/优雅退出用）。"""
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=timeout)
