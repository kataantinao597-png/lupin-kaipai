"""Phase 5（131-138）：AI 可观测性与成本控制。

与网关组约定：网关在调用前后分别调用 on_call_start / on_call_end，
本模块负责记录（ai_calls 表）、成本估算、熔断、并发、缓存。

- 记录走后台守护线程写库，不阻塞请求路径（约束：不影响调用性能）。
- 熔断/并发/缓存为内存状态（Phase 8 再持久化到 Redis）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue as _queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .ai_service import AIFatalError, AIRetryableError

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============ 135：模型调用熔断（内存） ============

BREAKER_FAIL_THRESHOLD = 5      # 连续失败 5 次熔断
BREAKER_OPEN_SECONDS = 300      # 熔断 5 分钟

_breakers: dict[str, dict] = {}
_breaker_lock = threading.Lock()


def _breaker_state(model: str) -> dict:
    with _breaker_lock:
        st = _breakers.get(model)
        if st is None:
            st = {"failures": 0, "opened_at": None}
            _breakers[model] = st
        return st


def breaker_check(model: str) -> None:
    """熔断中则直接抛错（不发起请求）。半开：窗口过期后允许试探一次。"""
    st = _breaker_state(model)
    with _breaker_lock:
        opened_at = st["opened_at"]
        if opened_at is None:
            return
        if time.time() - opened_at >= BREAKER_OPEN_SECONDS:
            # 半开：允许一次试探，成功则恢复
            st["opened_at"] = None
            return
        remain = int(BREAKER_OPEN_SECONDS - (time.time() - opened_at))
    raise AIFatalError(
        f"模型 {model} 熔断中（连续失败 {BREAKER_FAIL_THRESHOLD} 次），"
        f"约 {remain} 秒后恢复。请稍后重试或更换模型。")


def breaker_success(model: str) -> None:
    with _breaker_lock:
        st = _breakers.get(model)
        if st:
            st["failures"] = 0
            st["opened_at"] = None


def breaker_failure(model: str) -> None:
    with _breaker_lock:
        st = _breakers.get(model)
        if st is None:
            st = {"failures": 0, "opened_at": None}
            _breakers[model] = st
        st["failures"] += 1
        if st["failures"] >= BREAKER_FAIL_THRESHOLD:
            st["opened_at"] = time.time()
            logger.warning("模型 %s 熔断：连续失败 %d 次，暂停 %d 秒",
                           model, st["failures"], BREAKER_OPEN_SECONDS)


def breaker_status(model: str) -> dict:
    """调试/自检用：查看熔断状态。"""
    st = _breaker_state(model)
    with _breaker_lock:
        return {"model": model, "failures": st["failures"],
                "open": st["opened_at"] is not None}


# ============ 136：按模型并发控制 ============

MAX_CONCURRENT_PER_MODEL = 10   # 每模型默认最大并发
ACQUIRE_TIMEOUT = 30            # 排队最多等 30 秒

_semaphores: dict[str, threading.Semaphore] = {}
_sem_lock = threading.Lock()


def _semaphore_for(model: str, limit: int = MAX_CONCURRENT_PER_MODEL) -> threading.Semaphore:
    with _sem_lock:
        sem = _semaphores.get(model)
        if sem is None:
            sem = threading.Semaphore(limit)
            _semaphores[model] = sem
        return sem


def set_concurrency_limit(model: str, limit: int) -> None:
    """测试/运维用：调整某模型的并发上限。"""
    with _sem_lock:
        _semaphores[model] = threading.Semaphore(limit)


# ============ 138：结果缓存（内存，1 小时） ============

CACHE_TTL_SECONDS = 3600

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

# 输入含这些字段名时视为可能携带敏感差异，不缓存
_SENSITIVE_KEYS = ("api_key", "apikey", "secret", "password", "token",
                   "authorization", "cookie")


def _looks_sensitive(payload: dict) -> bool:
    """检查 payload 的字段名是否含敏感键（只查键名，避免正文误伤）。"""
    try:
        def _keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield str(k).lower()
                    yield from _keys(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _keys(v)
        return any(any(p in k for p in _SENSITIVE_KEYS) for k in _keys(payload))
    except Exception:
        return True


def make_cache_key(task_type: str, model: str, model_version: Any,
                   payload: dict) -> str:
    """缓存键 = task_type + 输入哈希 + 模型 + 模型版本。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"aicache:{task_type}:{model}:v{model_version}:{digest}"


def cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.time() > expires_at:
            _cache.pop(key, None)
            return None
        return value


def cache_put(key: str, value: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)
        # 简单容量保护：超 2000 条清掉最早过期的
        if len(_cache) > 2000:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                _cache.pop(k, None)


def cache_invalidate_model(model: str) -> int:
    """模型版本变更/价格变更时可按模型清缓存。返回清除条数。"""
    with _cache_lock:
        keys = [k for k in _cache if k.split(":")[2] == model]
        for k in keys:
            _cache.pop(k, None)
        return len(keys)


# ============ 132：成本估算 ============

# 内存价格缓存：model -> (input_per_1k, output_per_1k, version)
_price_cache: dict[str, tuple[float, float, int]] = {}
_price_lock = threading.Lock()

# 种子默认价（CNY/1k tokens）；价格未知时先用占位，运维可改表
_SEED_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.010, 0.030),
    "gpt-5.6-luna": (0.020, 0.060),
}


def seed_model_prices() -> None:
    """幂等：model_prices 为空时写入种子价格。"""
    try:
        from .auth.database import get_session_factory
        from .models import ModelPrice
    except Exception as e:  # noqa: BLE001
        logger.warning("价格种子跳过（DB 不可用）: %s", e)
        return
    db = get_session_factory()()
    try:
        exists = db.query(ModelPrice.id).first() is not None
        if not exists:
            for model, (ip, op) in _SEED_PRICES.items():
                db.add(ModelPrice(model=model, input_price_per_1k=ip,
                                  output_price_per_1k=op, currency="CNY",
                                  version=1))
            db.commit()
            logger.info("model_prices 种子价格已写入")
    except Exception as e:  # noqa: BLE001
        logger.warning("价格种子写入失败: %s", e)
        db.rollback()
    finally:
        db.close()


def get_model_price(model: str) -> tuple[float, float, int]:
    """返回 (input_per_1k, output_per_1k, version)；查不到用种子/0。"""
    with _price_lock:
        hit = _price_cache.get(model)
        if hit:
            return hit
    try:
        from sqlalchemy import desc
        from .auth.database import get_session_factory
        from .models import ModelPrice
        db = get_session_factory()()
        try:
            row = (db.query(ModelPrice)
                   .filter(ModelPrice.model == model)
                   .order_by(desc(ModelPrice.version)).first())
            if row:
                val = (float(row.input_price_per_1k),
                       float(row.output_price_per_1k), int(row.version))
                with _price_lock:
                    _price_cache[model] = val
                return val
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取模型价格失败，使用种子: %s", e)
    seed = _SEED_PRICES.get(model, (0.0, 0.0))
    return seed[0], seed[1], 1


def estimate_cost_cents(model: str, prompt_tokens: int,
                        completion_tokens: int) -> tuple[int, int]:
    """132：按 token 估算成本。返回 (cost_cents, price_version)。"""
    ip, op, version = get_model_price(model)
    cost = (prompt_tokens / 1000.0) * ip + (completion_tokens / 1000.0) * op
    return int(round(cost * 100)), version


# ============ 131：后台记录线程 ============

_record_q: "_queue.Queue[dict]" = _queue.Queue(maxsize=10000)
_writer_thread: Optional[threading.Thread] = None
_writer_lock = threading.Lock()


def _write_one(rec: dict) -> None:
    from .auth.database import get_session_factory
    from .models import AICall
    db = get_session_factory()()
    try:
        db.add(AICall(**rec))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_calls 记录失败: %s", e)
        db.rollback()
    finally:
        db.close()


def _writer_loop() -> None:
    seed_model_prices()
    while True:
        rec = _record_q.get()
        try:
            _write_one(rec)
        except Exception:  # noqa: BLE001
            logger.exception("ai_calls 后台写入异常")


def _ensure_writer() -> None:
    global _writer_thread
    if _writer_thread is None or not _writer_thread.is_alive():
        with _writer_lock:
            if _writer_thread is None or not _writer_thread.is_alive():
                _writer_thread = threading.Thread(
                    target=_writer_loop, name="lupin-ai-recorder",
                    daemon=True)
                _writer_thread.start()


def record_call_async(rec: dict) -> None:
    """非阻塞入队；队列满则丢弃并告警（绝不阻塞请求路径）。"""
    _ensure_writer()
    try:
        _record_q.put_nowait(rec)
    except _queue.Full:
        logger.warning("ai_calls 记录队列已满，丢弃一条记录 task=%s",
                       rec.get("task_type"))


# ============ 网关约定钩子：on_call_start / on_call_end ============

def on_call_start(*, task_type: str, model: str,
                  input_payload: Optional[dict] = None,
                  cacheable: bool = True) -> dict:
    """调用前钩子。

    做：熔断检查 → 并发位获取（最多等 30s）→ 缓存查。
    返回 ctx；若 ctx["cached_result"] 非 None，调用方直接返回缓存。
    熔断/并发超限时直接抛错（不发起请求）。
    """
    payload = input_payload or {}
    ip, op, version = get_model_price(model)
    ctx: dict[str, Any] = {
        "task_type": task_type, "model": model,
        "model_version": version, "start_ts": time.time(),
        "cache_key": None, "semaphore": None,
        "cached_result": None,
    }
    sensitive = _looks_sensitive(payload)
    if cacheable and not sensitive:
        key = make_cache_key(task_type, model, version, payload)
        ctx["cache_key"] = key
        hit = cache_get(key)
        if hit is not None:
            ctx["cached_result"] = hit
            return ctx
    # 熔断检查（缓存命中不走熔断/并发）
    breaker_check(model)
    sem = _semaphore_for(model)
    if not sem.acquire(timeout=ACQUIRE_TIMEOUT):
        raise AIRetryableError(
            f"模型 {model} 并发超限（{ACQUIRE_TIMEOUT}s 未获得执行位），"
            "请稍后重试")
    ctx["semaphore"] = sem
    return ctx


def on_call_end(ctx: dict, *, success: bool,
                usage: Optional[dict] = None,
                error: str = "",
                task_id: Optional[str] = None,
                cached: bool = False) -> None:
    """调用后钩子：熔断状态更新 + 后台记录 + 缓存写入由调用方完成。"""
    model = ctx["model"]
    sem = ctx.get("semaphore")
    try:
        if cached:
            # 缓存命中：记一条 cached 记录（不计 token/费用）
            record_call_async({
                "task_id": task_id, "task_type": ctx["task_type"],
                "model": model, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cost_cents": 0, "status": "cached",
                "error": None, "latency_ms": 0, "cached": True,
                "created_at": _utcnow(),
            })
            return
        if success:
            breaker_success(model)
        else:
            breaker_failure(model)
        usage = usage or {}
        pt = int(usage.get("prompt_tokens", 0) or 0)
        ct = int(usage.get("completion_tokens", 0) or 0)
        tt = int(usage.get("total_tokens", 0) or (pt + ct))
        cost_cents, _ = estimate_cost_cents(model, pt, ct)
        latency_ms = int((time.time() - ctx["start_ts"]) * 1000)
        record_call_async({
            "task_id": task_id, "task_type": ctx["task_type"],
            "model": model, "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": tt, "cost_cents": cost_cents,
            "status": "success" if success else "failed",
            "error": (error or "")[:500] or None,
            "latency_ms": latency_ms, "cached": False,
            "created_at": _utcnow(),
        })
    finally:
        if sem is not None:
            sem.release()


def run_observed(*, task_type: str, model: str, payload: dict,
                 do_call: Callable[[], tuple],
                 cacheable: bool = True,
                 task_id: Optional[str] = None):
    """AIService.chat 的统一包装：缓存 → 熔断 → 并发 → 调用 → 记录。

    do_call() -> (content, usage)。返回 (content, usage)；缓存命中时
    usage 为调用时的原始 usage 字典。
    """
    ctx = on_call_start(task_type=task_type, model=model,
                        input_payload=payload, cacheable=cacheable)
    if ctx["cached_result"] is not None:
        on_call_end(ctx, success=True, cached=True, task_id=task_id)
        return ctx["cached_result"]
    try:
        content, usage = do_call()
    except Exception as e:
        on_call_end(ctx, success=False, error=str(e), task_id=task_id)
        raise
    # 成功：写缓存（调用方传入的 payload 已做敏感检查）
    if ctx["cache_key"]:
        cache_put(ctx["cache_key"], (content, usage))
    on_call_end(ctx, success=True, usage=usage, task_id=task_id)
    return content, usage
