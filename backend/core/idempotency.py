"""V2.1 #3（Phase 7 改为 DB 实现）：AI 生成接口的 Idempotency-Key 去重。

语义（与内存版一致，保持接口兼容）：
- 客户端在请求头 ``Idempotency-Key`` 携带本次"用户意图"的唯一键（建议 UUID，
  每次用户主动点击生成新 key；失败后重试由用户再次点击产生新 key）。
- 相同 key + 相同请求体在 TTL 内到达 → 直接回放首次成功结果，
  不重复调模型、不重复扣费、不重复建异步任务。
- 相同 key + 不同请求体 → 400 幂等键冲突（防止 key 被误复用）。
- 并发双击：第二个请求会轮询等待第一个完成（最多等 ``WAIT_TIMEOUT``），
  然后回放结果；第一个若失败（abort），第二个自动接管执行。

存储（Phase 7）：
- ``idempotency_keys`` 表（迁移 0021）：多 worker / 重启后依然有效。
- scope（通常是 org_id）折叠进 body_hash（沿用内存版口径）；org_id 列
  仅作归属审计，调用方可通过可选 ``org_id`` 参数显式传入。
- running 行若存活超过 WAIT_TIMEOUT 视为僵尸（持有者进程已死），
  后来者可接管删除并重新执行——不伪造成功、不吞错。

公开接口（调用方无需改动）：
``idempotency_begin(route, request, body, scope="")`` /
``_Claim.replay / .active / .finish(resp) / .abort()`` /
``idempotency_invalidate(route, key)`` / ``reset_idempotency_store()``。
"""
from __future__ import annotations

# 导入顺序约定（仓库既有坑，非本 Phase 引入）：
# backend.models 不能作为进程内第一个 backend.* 导入，先走 backend.auth.*。
import backend.auth.database  # noqa: F401  (只为触发安全的导入顺序)

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import IdempotencyKey

from .error_codes import IDEMPOTENCY_KEY_CONFLICT
from .exceptions import BusinessError

IDEMPOTENCY_HEADER = "Idempotency-Key"

#: 回放记录保留时长（秒）：24 小时
TTL_SECONDS = 24 * 3600
#: 遗留常量：内存版的记录上限；DB 版改为 TTL 到期即删（见 _prune_expired），
#: 保留该名字仅为兼容旧导入。
MAX_RECORDS = 1000
#: 并发等待第一个请求完成的最长秒数（AI 生成约 120s 超时，留余量）；
#: running 行存活超过此时长视为僵尸，可被接管。
WAIT_TIMEOUT = 180.0
#: 等待中的轮询间隔（秒）
_POLL_INTERVAL = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(created_at: Optional[datetime]) -> float:
    if created_at is None:
        return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (_utcnow() - created_at).total_seconds()


def _session_factory():
    return backend.auth.database.get_session_factory()


@contextmanager
def _db() -> Iterator[Session]:
    """幂等表走独立短会话：调用方无需传入 db，多 worker 下各自可见。"""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()


def _body_hash(body: Dict[str, Any], scope: str = "") -> str:
    """与内存版完全一致的哈希口径（canonical JSON 的 sha256）。"""
    canonical = json.dumps({"body": body, "scope": scope or ""},
                           sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expired_row(row: IdempotencyKey) -> bool:
    return row.status == "done" and _age_seconds(row.created_at) > TTL_SECONDS


def _stale_running(row: IdempotencyKey) -> bool:
    """running 行存活超过 WAIT_TIMEOUT → 持有者大概率已死，可接管。"""
    return row.status == "running" and _age_seconds(row.created_at) > WAIT_TIMEOUT


def _prune_expired(db: Session) -> None:
    """顺手清理过期 done 行（TTL 到期即删，替代内存版的 MAX_RECORDS 淘汰）。"""
    cutoff = _utcnow()
    # 用 _age_seconds 口径比较：created_at 早于 TTL 秒前
    rows = (db.query(IdempotencyKey.id, IdempotencyKey.created_at)
              .filter(IdempotencyKey.status == "done")
              .limit(200).all())
    dead = [r.id for r in rows
            if _age_seconds(r.created_at) > TTL_SECONDS]
    if dead:
        (db.query(IdempotencyKey)
           .filter(IdempotencyKey.id.in_(dead))
           .delete(synchronize_session=False))
        db.commit()
    _ = cutoff  # 占位：保持将来按时间列裁剪的可扩展性


def reset_idempotency_store() -> None:
    """清空幂等记录（仅测试用）。表不存在时静默（迁移前调用不炸）。"""
    from sqlalchemy.exc import OperationalError
    try:
        with _db() as db:
            db.query(IdempotencyKey).delete(synchronize_session=False)
            db.commit()
    except OperationalError:
        pass


def idempotency_invalidate(route: str, key: str) -> None:
    """删除一条幂等记录（回放目标已失效时用，不抛错）。"""
    raw = (key or "").strip()
    if not raw:
        return
    with _db() as db:
        (db.query(IdempotencyKey)
           .filter(IdempotencyKey.route == route,
                   IdempotencyKey.key == raw)
           .delete(synchronize_session=False))
        db.commit()


class _Claim:
    """一次幂等声明：要么直接拿到回放结果，要么获得执行权。

    row_id 绑定本次声明抢到的具体行：finish/abort 只操作"自己抢到的那行"，
    避免把后来者接管后的新行误标记。
    """

    def __init__(self, route: str, key: str, row_id: Optional[str],
                 replay: Optional[Dict[str, Any]]):
        self._route = route
        self._key = key
        self._row_id = row_id
        self.replay = replay          # 非 None → 直接返回它，不要执行
        self.active = row_id is not None  # True → 必须 finish() 或 abort()

    def finish(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """执行成功：落盘回放记录并唤醒等待者。返回 response 本身。"""
        if not self.active:
            return response
        # JSON 往返一次，保证回放时可序列化且与原对象解耦
        snapshot = json.loads(json.dumps(response, ensure_ascii=False))
        with _db() as db:
            row = db.get(IdempotencyKey, self._row_id)
            if row is not None and row.status == "running":
                row.status = "done"
                row.response_json = json.dumps(snapshot, ensure_ascii=False)
                row.created_at = _utcnow()
                db.commit()
            # 行已被删除/接管：静默（不伪造成功，不抛错）
        return response

    def abort(self) -> None:
        """执行失败：删除自己抢到的记录（等待者会自动接管执行）。"""
        if not self.active:
            return
        with _db() as db:
            row = db.get(IdempotencyKey, self._row_id)
            if row is not None and row.status == "running":
                db.delete(row)
                db.commit()


def _replay_of(row: IdempotencyKey) -> Dict[str, Any]:
    replay = dict(json.loads(row.response_json or "{}"))
    replay["idempotent_replay"] = True
    return replay


def idempotency_begin(route: str, request: Request,
                      body: Dict[str, Any], scope: str = "",
                      org_id: Optional[str] = None) -> _Claim:
    """幂等入口（DB 版，语义与内存版一致）。

    - 请求头无 ``Idempotency-Key`` → 返回 ``_Claim(replay=None, active=False)``，
      调用方直接执行、不记录。
    - 命中已完成记录（同 key 同体，TTL 内）→ ``claim.replay`` 为回放结果
      （含 ``"idempotent_replay": True`` 标记），调用方直接返回。
    - 同 key 不同体 → 抛 ``BusinessError``（400，码 IDEMPOTENCY_KEY_CONFLICT）。
    - 未命中 → 返回 active 的 claim；调用方执行成功调 ``finish(resp)``，
      失败调 ``abort()`` 再抛错。
    - 并发命中"执行中"记录 → 轮询等待其完成（超时或失败则自动接管执行）。
    - ``org_id`` 为可选归属审计列；不传不影响去重语义。
    """
    raw_key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not raw_key:
        return _Claim(route, "", None, None)
    if len(raw_key) > 128:
        # 错误信息不回显 key 本体（防密钥片段进日志/响应）
        raise BusinessError("幂等键过长（最多 128 字符）")
    digest = _body_hash(body, scope)

    deadline = time.time() + WAIT_TIMEOUT
    while True:
        with _db() as db:
            _prune_expired(db)
            row = (db.query(IdempotencyKey)
                     .filter(IdempotencyKey.route == route,
                             IdempotencyKey.key == raw_key)
                     .first())
            if row is None:
                # 抢执行权：唯一约束 (route, key) 保证只有一个赢家
                row = IdempotencyKey(key=raw_key, org_id=org_id,
                                     route=route, body_hash=digest,
                                     status="running")
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    continue  # 并发竞争落败：循环回去重读
                db.refresh(row)
                return _Claim(route, raw_key, row.id, None)
            if _expired_row(row):
                db.delete(row)
                db.commit()
                continue
            if row.body_hash != digest:
                raise BusinessError(
                    "该幂等键已用于另一份不同的请求内容，请更换幂等键后重试",
                    code=IDEMPOTENCY_KEY_CONFLICT)
            if row.status == "done":
                return _Claim(route, raw_key, None, _replay_of(row))
            # 执行中：僵尸行直接接管
            if _stale_running(row):
                db.delete(row)
                db.commit()
                continue
        # 执行中：锁外轮询等待，避免阻塞其他 key
        remaining = deadline - time.time()
        if remaining <= 0:
            # 等太久：删掉旧行自己接管（与内存版一致，不静默）
            with _db() as db:
                old = (db.query(IdempotencyKey)
                         .filter(IdempotencyKey.route == route,
                                 IdempotencyKey.key == raw_key,
                                 IdempotencyKey.status == "running")
                         .first())
                if old is not None:
                    db.delete(old)
                    db.commit()
            continue
        time.sleep(min(remaining, _POLL_INTERVAL))


__all__ = [
    "IDEMPOTENCY_HEADER",
    "TTL_SECONDS",
    "MAX_RECORDS",
    "idempotency_begin",
    "idempotency_invalidate",
    "reset_idempotency_store",
]
