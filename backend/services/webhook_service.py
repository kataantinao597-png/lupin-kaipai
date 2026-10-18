"""S10 Webhook：组织事件订阅与投递。

事件：task.completed / review.expired / budget.exceeded。
投递：HMAC-SHA256 签名（X-Lupin-Signature: sha256=<hex>），失败重试 3 次
（指数退避，默认 60s/120s/240s，可用 LUPIN_WEBHOOK_RETRY_BASE_SEC 覆盖），
3 次失败后进死信（status=dead），可在管理 API 查看。

安全：
- 只允许 http/https；解析域名后拒绝私网/回环/链路本地/组播/保留地址
  （防 SSRF；DNS 解析存在 TOCTOU，重试/高安全场景建议走 egress 代理）。
- secret 只存库，签名在服务端计算，不回传。

投递是 best-effort：调用方（任务队列/网关/审核失效）用 try/except 包裹，
绝不因 webhook 失败影响主流程。接收方应把事件当"提示"，以 API 查询为准。
"""
import hashlib
import heapq
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import models

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = ("task.completed", "review.expired", "budget.exceeded")
MAX_ATTEMPTS = 3
SIGNATURE_HEADER = "X-Lupin-Signature"
EVENT_HEADER = "X-Lupin-Event"
DELIVERY_HEADER = "X-Lupin-Delivery-Id"
REQUEST_TIMEOUT = 10  # 秒


def _retry_base_seconds() -> int:
    try:
        return max(1, int(os.environ.get("LUPIN_WEBHOOK_RETRY_BASE_SEC", "60")))
    except ValueError:
        return 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SSRF 防护
# ---------------------------------------------------------------------------

def _assert_url_safe(url: str) -> str:
    """校验 webhook URL；不安全抛 ValueError。返回规范化后的 URL。"""
    from urllib.parse import urlparse
    try:
        parts = urlparse(url.strip())
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"URL 非法: {e}")
    if parts.scheme not in ("http", "https"):
        raise ValueError("只允许 http/https 协议的 webhook 地址")
    host = parts.hostname or ""
    if not host or host.lower() == "localhost":
        raise ValueError("不允许的 webhook 目标主机")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"域名解析失败: {e}")
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError(f"解析出非法 IP: {ip}")
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved
                or addr.is_unspecified):
            raise ValueError(f"不允许的 webhook 目标地址: {ip}（内网/保留地址）")
    return url.strip()


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 签名，格式 sha256=<hex>。"""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def generate_secret() -> str:
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# 投递执行
# ---------------------------------------------------------------------------

def _post_delivery(url: str, secret: str, event_type: str,
                   delivery_id: str, payload: dict) -> None:
    """单次投递；失败抛异常。httpx 可被测试 monkeypatch。"""
    import httpx
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(secret, body),
        EVENT_HEADER: event_type,
        DELIVERY_HEADER: delivery_id,
        "User-Agent": "lupin-kaipai-webhook/1.0",
    }
    resp = httpx.post(url, content=body, headers=headers,
                      timeout=REQUEST_TIMEOUT, follow_redirects=False)
    if resp.status_code >= 400:
        raise RuntimeError(f"webhook 返回 {resp.status_code}")


def _attempt_delivery(delivery_id: str, post_fn=None) -> bool:
    """执行一次投递尝试并更新投递记录。返回 True=已送达。

    post_fn 供测试注入；默认走真实 HTTP。
    """
    from ..auth.database import get_session_factory
    db = get_session_factory()()
    try:
        d = db.get(models.WebhookDelivery, delivery_id)
        if d is None or d.status == "sent":
            return d is not None
        wh = db.get(models.Webhook, d.webhook_id)
        if wh is None or not wh.is_active:
            d.status = "dead"
            d.last_error = "webhook 已删除或停用"
            db.commit()
            return False
        payload = json.loads(d.payload_json or "{}")
        d.attempts = (d.attempts or 0) + 1
        try:
            (post_fn or _post_delivery)(wh.url, wh.secret, d.event_type,
                                        d.id, payload)
        except Exception as e:  # noqa: BLE001
            d.last_error = f"{type(e).__name__}: {e}"[:500]
            if d.attempts >= MAX_ATTEMPTS:
                d.status = "dead"
                logger.warning("webhook 投递进死信 delivery=%s webhook=%s: %s",
                               d.id, wh.id, d.last_error)
            else:
                d.status = "pending"
                backoff = _retry_base_seconds() * (2 ** (d.attempts - 1))
                d.next_retry_at = _utcnow() + timedelta(seconds=backoff)
                _dispatcher.enqueue(d.id, d.next_retry_at)
            db.commit()
            return False
        d.status = "sent"
        d.delivered_at = _utcnow()
        d.last_error = None
        db.commit()
        return True
    finally:
        db.close()


class _Dispatcher:
    """后台投递调度器：单守护线程 + 最小堆（按 next_retry_at）。"""

    def __init__(self):
        self._heap = []  # (timestamp, delivery_id)
        self._cond = threading.Condition()
        self._thread = None
        self._lock = threading.Lock()

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run,
                                                name="webhook-dispatcher",
                                                daemon=True)
                self._thread.start()

    def enqueue(self, delivery_id: str, when: datetime = None):
        self._ensure_thread()
        ts = when.timestamp() if when else time.time()
        with self._cond:
            heapq.heappush(self._heap, (ts, delivery_id))
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._heap:
                    self._cond.wait()
                ts, delivery_id = self._heap[0]
                wait = ts - time.time()
                if wait > 0:
                    self._cond.wait(timeout=wait)
                    continue
                heapq.heappop(self._heap)
            try:
                _attempt_delivery(delivery_id)
            except Exception:  # noqa: BLE001
                logger.exception("webhook 调度器单次投递异常 delivery=%s",
                                 delivery_id)


_dispatcher = _Dispatcher()


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def dispatch_event(event_type: str, org_id: str, payload: dict) -> int:
    """发布组织事件：为每个订阅该事件的活跃 webhook 建投递记录并入队。

    返回命中的 webhook 数。内部异常不抛出（调用方也应用 try/except 包裹）。
    """
    if event_type not in WEBHOOK_EVENTS:
        logger.warning("未知 webhook 事件类型: %s", event_type)
        return 0
    from ..auth.database import get_session_factory
    db = get_session_factory()()
    try:
        hooks = (
            db.query(models.Webhook)
            .filter(models.Webhook.org_id == org_id,
                    models.Webhook.is_active.is_(True))
            .all()
        )
        count = 0
        envelope = {
            "event": event_type,
            "org_id": org_id,
            "occurred_at": _utcnow().isoformat(),
            "data": payload,
        }
        body = json.dumps(envelope, ensure_ascii=False)
        for wh in hooks:
            try:
                events = json.loads(wh.events_json or "[]")
            except (ValueError, TypeError):
                events = []
            if event_type not in events:
                continue
            d = models.WebhookDelivery(
                webhook_id=wh.id, event_type=event_type, payload_json=body)
            db.add(d)
            db.flush()
            _dispatcher.enqueue(d.id)
            count += 1
        db.commit()
        return count
    finally:
        db.close()


def parse_events(raw) -> list:
    """校验事件列表；非法抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("events 必须是数组")
    events = [str(e) for e in raw]
    unknown = [e for e in events if e not in WEBHOOK_EVENTS]
    if unknown:
        raise ValueError(f"未知事件类型: {unknown}；可选 {list(WEBHOOK_EVENTS)}")
    if not events:
        raise ValueError("events 不能为空")
    return events
