"""V2.1 #3：AI 生成接口的 Idempotency-Key 去重。

语义：
- 客户端在请求头 ``Idempotency-Key`` 携带本次"用户意图"的唯一键（建议 UUID，
  每次用户主动点击生成新 key；失败后重试由用户再次点击产生新 key）。
- 相同 key + 相同请求体在 TTL 内到达 → 直接回放首次成功结果，
  不重复调模型、不重复扣费、不重复建异步任务。
- 相同 key + 不同请求体 → 400 幂等键冲突（防止 key 被误复用）。
- 并发双击：第二个请求会等待第一个完成（最多等 ``WAIT_TIMEOUT``），
  然后回放结果；第一个若失败，第二个自动接管执行。

存储说明：
- 进程内内存（TTL 24h，上限 1000 条，超限淘汰最旧）。
- 不建新表（V2.1 阶段不改 models/migrations），覆盖双击/重复提交等
  主力场景。已知局限：多 worker 进程之间、重启后记录丢失，
  届时会退化为"各自执行一次"——不伪造成功、不吞错。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import Request

from .error_codes import IDEMPOTENCY_KEY_CONFLICT
from .exceptions import BusinessError

IDEMPOTENCY_HEADER = "Idempotency-Key"

#: 回放记录保留时长（秒）：24 小时
TTL_SECONDS = 24 * 3600
#: 内存记录上限：超限时淘汰最旧
MAX_RECORDS = 1000
#: 并发等待第一个请求完成的最长秒数（AI 生成约 120s 超时，留余量）
WAIT_TIMEOUT = 180.0


class _Entry:
    __slots__ = ("body_hash", "status", "response", "created_at", "event")

    def __init__(self, body_hash: str):
        self.body_hash = body_hash
        self.status = "running"          # running | done
        self.response: Optional[Dict[str, Any]] = None
        self.created_at = time.time()
        self.event = threading.Event()


_store: "OrderedDict[str, _Entry]" = OrderedDict()
_lock = threading.Lock()


def _store_key(route: str, key: str) -> str:
    return f"{route}\x00{key}"


def _body_hash(body: Dict[str, Any], scope: str = "") -> str:
    canonical = json.dumps({"body": body, "scope": scope or ""},
                           sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expired(entry: _Entry, now: float) -> bool:
    return entry.status == "done" and (now - entry.created_at) > TTL_SECONDS


def reset_idempotency_store() -> None:
    """清空内存记录（仅测试用）。"""
    with _lock:
        _store.clear()


def idempotency_invalidate(route: str, key: str) -> None:
    """删除一条幂等记录（回放目标已失效时用，不抛错）。"""
    raw = (key or "").strip()
    if not raw:
        return
    with _lock:
        _store.pop(_store_key(route, raw), None)


class _Claim:
    """一次幂等声明：要么直接拿到回放结果，要么获得执行权。"""

    def __init__(self, store_key: str, entry: Optional[_Entry],
                 replay: Optional[Dict[str, Any]]):
        self._store_key = store_key
        self._entry = entry
        self.replay = replay          # 非 None → 直接返回它，不要执行
        self.active = entry is not None  # True → 必须 finish() 或 abort()

    def finish(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """执行成功：落盘回放记录并唤醒等待者。返回 response 本身。"""
        if not self.active:
            return response
        # JSON 往返一次，保证回放时可序列化且与原对象解耦
        snapshot = json.loads(json.dumps(response, ensure_ascii=False))
        with _lock:
            entry = _store.get(self._store_key)
            if entry is self._entry:
                entry.status = "done"
                entry.response = snapshot
                entry.created_at = time.time()
                entry.event.set()
        return response

    def abort(self) -> None:
        """执行失败：删除记录并唤醒等待者（等待者会自动接管执行）。"""
        if not self.active:
            return
        with _lock:
            entry = _store.get(self._store_key)
            if entry is self._entry:
                del _store[self._store_key]
                entry.event.set()


def idempotency_begin(route: str, request: Request,
                      body: Dict[str, Any], scope: str = "") -> _Claim:
    """幂等入口。

    - 请求头无 ``Idempotency-Key`` → 返回 ``_Claim(replay=None, active=False)``，
      调用方直接执行、不记录。
    - 命中已完成记录（同 key 同体）→ ``claim.replay`` 为回放结果
      （含 ``"idempotent_replay": True`` 标记），调用方直接返回。
    - 同 key 不同体 → 抛 ``BusinessError``（400，码 IDEMPOTENCY_KEY_CONFLICT）。
    - 未命中 → 返回 active 的 claim；调用方执行成功调 ``finish(resp)``，
      失败调 ``abort()`` 再抛错。
    - 并发命中"执行中"记录 → 等待其完成（超时或失败则自动接管执行）。
    """
    raw_key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not raw_key:
        return _Claim("", None, None)
    if len(raw_key) > 128:
        raise BusinessError("幂等键过长（最多 128 字符）")
    digest = _body_hash(body, scope)
    skey = _store_key(route, raw_key)

    deadline = time.time() + WAIT_TIMEOUT
    while True:
        with _lock:
            now = time.time()
            entry = _store.get(skey)
            if entry is not None and _expired(entry, now):
                del _store[skey]
                entry = None
            if entry is None:
                entry = _Entry(digest)
                _store[skey] = entry
                while len(_store) > MAX_RECORDS:
                    _store.popitem(last=False)
                return _Claim(skey, entry, None)
            if entry.body_hash != digest:
                raise BusinessError(
                    "该幂等键已用于另一份不同的请求内容，请更换幂等键后重试",
                    code=IDEMPOTENCY_KEY_CONFLICT)
            if entry.status == "done":
                replay = dict(entry.response or {})
                replay["idempotent_replay"] = True
                return _Claim(skey, None, replay)
            running_entry = entry
        # 执行中：等待完成（锁外等待，避免阻塞其他 key）
        remaining = deadline - time.time()
        if remaining <= 0:
            # 等太久：视为异常，删掉旧记录自己接管（不静默）
            with _lock:
                if _store.get(skey) is running_entry:
                    del _store[skey]
            continue
        running_entry.event.wait(timeout=min(remaining, 5.0))
        with _lock:
            entry = _store.get(skey)
            if entry is running_entry and entry.status == "done":
                replay = dict(entry.response or {})
                replay["idempotent_replay"] = True
                return _Claim(skey, None, replay)
            if entry is not running_entry or entry is None:
                # 执行者失败已 abort → 循环回去自己接管
                continue
            # 仍在 running → 继续等


__all__ = [
    "IDEMPOTENCY_HEADER",
    "TTL_SECONDS",
    "idempotency_begin",
    "idempotency_invalidate",
    "reset_idempotency_store",
]
