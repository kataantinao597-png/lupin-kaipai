"""V2.1 #3：生成接口 Idempotency-Key 幂等测试。

- 同 key + 同体 → 第二次直接回放，不重复调模型（不重复扣费）
- 同 key + 不同体 → 400 幂等键冲突（业务码 2007）
- 过期 key → 重新执行
- 无 key → 每次都执行（直通）
- 并发双击同 key → 模型只调一次
- ?async=true 入队：同 key 不重复建任务
"""
import os
import sys
import threading

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/idempotency.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.core import deps as core_deps
    if hasattr(core_deps.get_ai_service, "_singleton"):
        delattr(core_deps.get_ai_service, "_singleton")
    from backend.auth.database import reset_engine, get_session_factory
    from backend.media import reset_storage
    from backend.media.signing import reset_sign_secret
    from backend.core.idempotency import reset_idempotency_store
    from backend.security.rate_limit import reset_rate_limit_buckets
    reset_engine()
    reset_storage()
    reset_sign_secret()
    reset_idempotency_store()
    reset_rate_limit_buckets()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "heads")

    # V3.1 P0：生成接口强制登录——fixture 用真实 Bearer token（u1，组织 org1 的 owner）
    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    from backend.auth.security import create_access_token
    db0 = get_session_factory()()
    org1 = Organization(id="org-v31-test", name="幂等测试合作社",
                        created_by="u1")
    db0.add(org1)
    db0.add(AuthUser(id="u1", email="u1@test.local", password_hash="x"))
    db0.flush()
    db0.add(Membership(user_id="u1", org_id="org-v31-test", role="owner"))
    db0.commit()
    db0.close()
    token, _exp = create_access_token("u1")

    from fastapi.testclient import TestClient
    from backend.app import app
    client = TestClient(app, raise_server_exceptions=False,
                        headers={"Authorization": f"Bearer {token}"})
    yield client
    app.dependency_overrides.clear()
    reset_idempotency_store()
    reset_rate_limit_buckets()


class FakeGateway:
    """可数调用次数的假网关：返回固定改写结果。"""

    def __init__(self, delay: float = 0.0):
        self.calls = 0
        self._lock = threading.Lock()
        self.delay = delay

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            **kwargs):
        import time
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls += 1
        data = {"rewritten": "改写后的口语句子", "changes": ["更口语"]}
        errs, out = validate_fn(data)
        assert not errs, errs
        return out, {"attempts": 1}


@pytest.fixture()
def fake_gw(api_client):
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakeGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    return fake


BODY = {"text": "这款苹果口感脆甜，欢迎大家购买。", "product_name": "烟台苹果"}


class TestReplay:
    def test_same_key_same_body_replays(self, api_client, fake_gw):
        h = {"Idempotency-Key": "k-replay-1"}
        r1 = api_client.post("/api/colloquial-rewrite", json=BODY, headers=h)
        r2 = api_client.post("/api/colloquial-rewrite", json=BODY, headers=h)
        assert r1.status_code == 200 and r2.status_code == 200
        assert fake_gw.calls == 1  # 只调了一次模型：不重复扣费
        assert r2.json()["rewritten"] == r1.json()["rewritten"]
        assert r2.json().get("idempotent_replay") is True
        assert "idempotent_replay" not in r1.json()

    def test_same_key_different_body_conflict(self, api_client, fake_gw):
        h = {"Idempotency-Key": "k-conflict-1"}
        r1 = api_client.post("/api/colloquial-rewrite", json=BODY, headers=h)
        assert r1.status_code == 200
        other = dict(BODY, text="完全不同的文案内容")
        r2 = api_client.post("/api/colloquial-rewrite", json=other, headers=h)
        assert r2.status_code == 400
        data = r2.json()
        assert data.get("code") == 2007
        assert fake_gw.calls == 1  # 冲突请求没有触发第二次模型调用

    def test_no_key_passthrough(self, api_client, fake_gw):
        r1 = api_client.post("/api/colloquial-rewrite", json=BODY)
        r2 = api_client.post("/api/colloquial-rewrite", json=BODY)
        assert r1.status_code == 200 and r2.status_code == 200
        assert fake_gw.calls == 2  # 无 key：每次都执行
        assert "idempotent_replay" not in r2.json()

    def test_expired_key_reexecutes(self, api_client, fake_gw):
        from backend.core.idempotency import (
            _store, _store_key, TTL_SECONDS)
        h = {"Idempotency-Key": "k-expire-1"}
        r1 = api_client.post("/api/colloquial-rewrite", json=BODY, headers=h)
        assert r1.status_code == 200
        assert fake_gw.calls == 1
        # 把记录调成 25 小时前（超过 24h TTL）
        key = _store_key("colloquial-rewrite", "k-expire-1")
        _store[key].created_at -= (TTL_SECONDS + 3600)
        r2 = api_client.post("/api/colloquial-rewrite", json=BODY, headers=h)
        assert r2.status_code == 200
        assert fake_gw.calls == 2  # 过期 → 重新执行
        assert "idempotent_replay" not in r2.json()


class TestConcurrency:
    def test_double_click_calls_model_once(self, api_client):
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        fake = FakeGateway(delay=0.8)  # 第一次调用故意慢，模拟双击窗口
        app.dependency_overrides[get_ai_gateway] = lambda: fake
        h = {"Idempotency-Key": "k-concurrent-1"}
        results = []

        def _post():
            r = api_client.post("/api/colloquial-rewrite", json=BODY,
                                headers=h)
            results.append(r)

        t1 = threading.Thread(target=_post)
        t2 = threading.Thread(target=_post)
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)
        assert len(results) == 2
        assert all(r.status_code == 200 for r in results)
        assert fake.calls == 1  # 并发双击：模型只调一次
        flags = [r.json().get("idempotent_replay") is True for r in results]
        assert any(flags)  # 至少一个是回放


class TestEnqueue:
    def test_async_enqueue_idempotent(self, api_client, monkeypatch):
        # runner 不真跑：只验证"不重复建任务"
        from backend.ai_tasks import queue as qmod
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        monkeypatch.setattr(qmod.TaskQueue, "submit",
                            lambda self, *a, **k: None)
        # 异步入口的 gw 依赖在函数体之前求值：无 AI 配置会 503，mock 掉
        app.dependency_overrides[get_ai_gateway] = lambda: FakeGateway()
        body = {"product": {"product_name": "测试樱桃"}, "facts": [],
                "org_id": "org-v31-test"}  # V3.1 P0：异步建任务需登录 + 组织成员
        h = {"Idempotency-Key": "k-enqueue-1"}
        r1 = api_client.post("/api/ideas?async=true", json=body, headers=h)
        r2 = api_client.post("/api/ideas?async=true", json=body, headers=h)
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["task_id"] == r2.json()["task_id"]
        assert r2.json().get("idempotent_replay") is True
        # 数据库里只有一个任务
        from backend.auth.database import get_session_factory
        from backend.models import AITask
        from sqlalchemy import select, func
        db = get_session_factory()()
        try:
            n = db.scalar(select(func.count()).select_from(AITask))
        finally:
            db.close()
        assert n == 1


class TestStoreUnit:
    def _req(self, key=None):
        from starlette.requests import Request
        headers = []
        if key:
            headers.append((b"idempotency-key", key.encode()))
        return Request({"type": "http", "headers": headers})

    def test_no_header_inactive(self):
        from backend.core.idempotency import idempotency_begin
        claim = idempotency_begin("r", self._req(), {"a": 1})
        assert claim.replay is None and claim.active is False
        assert claim.finish({"ok": 1}) == {"ok": 1}  # 无 key：直通无记录

    def test_key_too_long(self):
        from backend.core.idempotency import idempotency_begin
        from backend.core.exceptions import BusinessError
        with pytest.raises(BusinessError):
            idempotency_begin("r", self._req("k" * 200), {"a": 1})

    def test_abort_allows_retry(self):
        from backend.core.idempotency import idempotency_begin
        c1 = idempotency_begin("r-abort", self._req("k-abort-1"), {"a": 1})
        assert c1.active and c1.replay is None
        c1.abort()  # 执行失败
        c2 = idempotency_begin("r-abort", self._req("k-abort-1"), {"a": 1})
        assert c2.active and c2.replay is None  # 可重新执行
        c2.finish({"ok": 2})
        c3 = idempotency_begin("r-abort", self._req("k-abort-1"), {"a": 1})
        assert c3.replay == {"ok": 2, "idempotent_replay": True}


if __name__ == "__main__":
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
