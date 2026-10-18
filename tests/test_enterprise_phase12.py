"""S10 企业管理与商业化脚手架回归测试（第十二阶段）。

- 用量报表聚合
- 配额 CRUD + 超限拦截（ai_calls_daily / members / storage_mb）
- Webhook CRUD + secret 一次返回 + SSRF 拦截 + 投递/重试/死信
- 模板 CRUD + 版本管理
- 计费：价格配置（不硬编码）+ 对账单 + 开账单
"""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration
from fastapi.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: E402  (先导入，绕开循环导入)
from backend.app import app  # noqa: E402
from backend import models  # noqa: E402
from backend.auth.database import (  # noqa: E402
    get_engine,
    get_session_factory,
    reset_engine,
)
from backend.auth.deps import get_current_user  # noqa: E402
from backend.auth.models import User as AuthUser  # noqa: E402
from backend.core.events import reset_event_bus  # noqa: E402
from backend.services import webhook_service as webhooks  # noqa: E402
from backend.services import quota_service as quotas  # noqa: E402
from backend.services import billing_service as billing  # noqa: E402

# fixture 会把 _assert_url_safe 打桩；此处缓存真实实现供 SSRF 测试用
_REAL_ASSERT_URL_SAFE = webhooks._assert_url_safe


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表到最新 head）。"""
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_WEBHOOK_RETRY_BASE_SEC", "60")
    # 测试期间禁用后台投递线程的重试入队（避免真实 HTTP）
    monkeypatch.setattr(webhooks._dispatcher, "enqueue",
                        lambda *a, **k: None)
    # 沙箱 DNS 把所有域名解析到保留网段；CRUD 测试跳过 SSRF 校验
    #（SSRF 逻辑由 test_ssrf_blocked 直接断言）
    monkeypatch.setattr(webhooks, "_assert_url_safe", lambda u: u.strip())
    reset_engine()
    reset_event_bus()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    get_engine()
    db = get_session_factory()()
    org = models.Organization(name="企业测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    db.add(AuthUser(id="u_owner", email="owner@test.local", password_hash="x"))
    db.add(AuthUser(id="u_member", email="member@test.local",
                    password_hash="x"))
    db.flush()
    db.add(models.Membership(user_id="u_owner", org_id=org.id, role="owner"))
    db.add(models.Membership(user_id="u_member", org_id=org.id,
                             role="editor"))
    db.commit()
    org_id = org.id
    db.close()

    state = {"uid": "u_owner", "email": "owner@test.local"}

    def _override_user():
        return AuthUser(id=state["uid"], email=state["email"],
                        password_hash="x")

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    def _set_user(uid, email):
        state["uid"] = uid
        state["email"] = email

    from backend.auth.database import get_db as _get_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[_get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    yield client, org_id, _set_user
    app.dependency_overrides.clear()
    reset_engine()
    reset_event_bus()


def _ok(r):
    assert r.status_code in (200, 201), f"{r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body["code"] == 0, f"code={body['code']}: {body}"
    return body["data"]


def _db():
    s = get_session_factory()()
    try:
        yield s
    finally:
        s.close()


def _seed_ai_call(org_id, tokens=1000, cost=3, model="m1", task_type="ideas"):
    for s in _db():
        t = models.AITask(org_id=org_id, created_by="u_owner",
                          task_type=task_type, status="succeeded")
        s.add(t)
        s.flush()
        s.add(models.AICall(task_id=t.id, task_type=task_type, model=model,
                            prompt_tokens=tokens, completion_tokens=0,
                            total_tokens=tokens, cost_cents=cost,
                            status="success"))
        s.commit()
        return


class TestQuotas:
    def test_crud_and_unlimited(self, api_client):
        client, org_id, _ = api_client
        data = _ok(client.get(f"/api/v1/enterprise/{org_id}/quotas"))
        assert {q["quota_type"] for q in data} == {
            "ai_calls_daily", "storage_mb", "members"}
        assert all(q["unlimited"] for q in data)

        q = _ok(client.put(f"/api/v1/enterprise/{org_id}/quotas",
                           json={"quota_type": "members", "limit_value": 5}))
        assert q["limit_value"] == 5 and not q["unlimited"]
        assert q["current_usage"] == 2  # owner + editor

        # 非法类型
        r = client.put(f"/api/v1/enterprise/{org_id}/quotas",
                       json={"quota_type": "nope", "limit_value": 1})
        assert r.status_code == 400

        # 取消限制
        q = _ok(client.put(f"/api/v1/enterprise/{org_id}/quotas",
                           json={"quota_type": "members", "limit_value": 0}))
        assert q["unlimited"]

    def test_non_admin_cannot_set(self, api_client):
        client, org_id, set_user = api_client
        set_user("u_member", "member@test.local")  # editor
        r = client.put(f"/api/v1/enterprise/{org_id}/quotas",
                       json={"quota_type": "members", "limit_value": 5})
        assert r.status_code == 403

    def test_members_quota_blocks_join(self, api_client):
        client, org_id, set_user = api_client
        _ok(client.put(f"/api/v1/enterprise/{org_id}/quotas",
                       json={"quota_type": "members", "limit_value": 2}))
        # 发邀请码
        inv = _ok(client.post(f"/api/v1/orgs/{org_id}/invite", json={}))
        # 第三个用户加入 → 403
        set_user("u_owner", "owner@test.local")
        for s in _db():
            s.add(AuthUser(id="u_new", email="new@test.local",
                           password_hash="x"))
            s.commit()
        set_user("u_new", "new@test.local")
        r = client.post("/api/v1/orgs/join", json={"code": inv["code"]})
        assert r.status_code == 403
        assert r.json()["code"] == 4021

    def test_ai_calls_quota_in_gateway(self, api_client):
        client, org_id, _ = api_client
        _ok(client.put(f"/api/v1/enterprise/{org_id}/quotas",
                       json={"quota_type": "ai_calls_daily", "limit_value": 1}))
        _seed_ai_call(org_id)  # 今日已用 1 次
        from backend.ai.gateway import AIGateway
        for s in _db():
            gw = AIGateway(service=object(), org_id=org_id, db=s)
            with pytest.raises(quotas.QuotaExceededError):
                gw._check_budget("ideas")
            # QuotaExceededError 是 BudgetExceededError 子类 → API 层 402
            from backend.ai.gateway import BudgetExceededError
            assert issubclass(quotas.QuotaExceededError, BudgetExceededError)

    def test_storage_quota_blocks_work_create(self, api_client):
        client, org_id, _ = api_client
        _ok(client.put(f"/api/v1/enterprise/{org_id}/quotas",
                       json={"quota_type": "storage_mb", "limit_value": 1}))
        for s in _db():
            s.add(models.Work(org_id=org_id, owner_id="u_owner",
                              title="大作品",
                              data_json="x" * (2 * 1024 * 1024)))
            s.commit()
        r = client.post("/api/v1/works",
                        json={"org_id": org_id, "title": "新作品",
                              "data": {"product": {"product_name": "葱"}}})
        assert r.status_code == 402
        assert r.json()["code"] == 4021


class TestWebhooks:
    def test_crud_secret_once(self, api_client):
        client, org_id, _ = api_client
        created = client.post(
            f"/api/v1/enterprise/{org_id}/webhooks",
            json={"url": "https://example.com/hook",
                  "events": ["task.completed"]})
        assert created.status_code == 200, created.text[:200]
        data = _ok(created)
        assert len(data["secret"]) == 64
        wh_id = data["id"]
        # 列表不再返回 secret
        items = _ok(client.get(f"/api/v1/enterprise/{org_id}/webhooks"))
        assert len(items) == 1 and "secret" not in items[0]
        # 更新
        upd = _ok(client.patch(
            f"/api/v1/enterprise/{org_id}/webhooks/{wh_id}",
            json={"events": ["task.completed", "budget.exceeded"]}))
        assert upd["events"] == ["task.completed", "budget.exceeded"]
        # 删除
        r = client.delete(f"/api/v1/enterprise/{org_id}/webhooks/{wh_id}")
        assert r.status_code == 204

    def test_ssrf_blocked(self, api_client):
        # 用模块导入时缓存的真实实现（fixture 里为 CRUD 打了桩）
        for bad in ("http://127.0.0.1/hook", "http://localhost/hook",
                    "http://10.0.0.5/hook", "http://[::1]/hook",
                    "ftp://example.com/hook"):
            with pytest.raises(ValueError):
                _REAL_ASSERT_URL_SAFE(bad)
        # 未知事件 / 非法格式
        with pytest.raises(ValueError):
            webhooks.parse_events(["nope"])
        with pytest.raises(ValueError):
            webhooks.parse_events("task.completed")

    def test_test_delivery_and_log(self, api_client, monkeypatch):
        client, org_id, _ = api_client
        calls = []

        def fake_post(url, secret, event_type, delivery_id, payload):
            calls.append((url, event_type, payload))

        monkeypatch.setattr(webhooks, "_post_delivery", fake_post)
        created = _ok(client.post(
            f"/api/v1/enterprise/{org_id}/webhooks",
            json={"url": "https://example.com/hook",
                  "events": ["task.completed"]}))
        data = _ok(client.post(
            f"/api/v1/enterprise/{org_id}/webhooks/{created['id']}/test"))
        assert data["ok"] is True
        assert calls and calls[0][1] == "webhook.test"

    def test_retry_then_dead(self, api_client, monkeypatch):
        client, org_id, _ = api_client
        created = _ok(client.post(
            f"/api/v1/enterprise/{org_id}/webhooks",
            json={"url": "https://example.com/hook",
                  "events": ["task.completed"]}))
        # 手工建一条投递记录
        for s in _db():
            d = models.WebhookDelivery(
                webhook_id=created["id"], event_type="task.completed",
                payload_json="{}")
            s.add(d)
            s.commit()
            did = d.id

        def boom(*a, **k):
            raise ConnectionError("nope")

        for _ in range(3):
            assert webhooks._attempt_delivery(did, post_fn=boom) is False
        for s in _db():
            d = s.get(models.WebhookDelivery, did)
            assert d.status == "dead" and d.attempts == 3
        # 送达成功路径
        for s in _db():
            d2 = models.WebhookDelivery(
                webhook_id=created["id"], event_type="task.completed",
                payload_json="{}")
            s.add(d2)
            s.commit()
            did2 = d2.id
        assert webhooks._attempt_delivery(
            did2, post_fn=lambda *a, **k: None) is True
        for s in _db():
            assert s.get(models.WebhookDelivery, did2).status == "sent"

    def test_signature(self):
        sig = webhooks.sign_payload("s3cret", b"{}")
        assert sig.startswith("sha256=") and len(sig) == 71


class TestTemplates:
    def test_crud_and_versions(self, api_client):
        client, org_id, set_user = api_client
        t = client.post(f"/api/v1/enterprise/{org_id}/templates",
                        json={"name": "口播模板", "category": "口播",
                              "content": {"shots": [1]}})
        data = _ok(t)
        assert data["version"] == 1
        tpl_id = data["id"]
        # 更新内容 → 版本+1
        upd = _ok(client.patch(
            f"/api/v1/enterprise/{org_id}/templates/{tpl_id}",
            json={"content": {"shots": [1, 2]}}))
        assert upd["version"] == 2
        vers = _ok(client.get(
            f"/api/v1/enterprise/{org_id}/templates/{tpl_id}/versions"))
        assert [v["version"] for v in vers] == [2, 1]
        detail = _ok(client.get(
            f"/api/v1/enterprise/{org_id}/templates/{tpl_id}"))
        assert detail["content"] == {"shots": [1, 2]}
        # 删除
        assert client.delete(
            f"/api/v1/enterprise/{org_id}/templates/{tpl_id}").status_code == 204

    def test_public_needs_review_todo(self, api_client):
        client, org_id, set_user = api_client
        set_user("u_member", "member@test.local")  # editor 非管理员
        t = _ok(client.post(
            f"/api/v1/enterprise/{org_id}/templates",
            json={"name": "想公开的模板", "content": {}, "is_public": True}))
        assert t["is_public"] is False  # 被降级为私有
        assert "notice" in t


class TestBilling:
    def test_seed_zero_and_statement(self, api_client):
        client, org_id, _ = api_client
        prices = _ok(client.post(
            f"/api/v1/enterprise/{org_id}/billing/prices/seed"))
        assert all(p["unit_price_cents"] == 0 for p in prices)
        assert all("运营" in p["note"] for p in prices)
        _seed_ai_call(org_id, tokens=2000, cost=6)
        stmt = _ok(client.get(
            f"/api/v1/enterprise/{org_id}/billing/statement"))
        assert stmt["amount_cents"] == 0
        assert stmt["prices_unset"] is True
        assert stmt["usage"]["ai"]["calls"] == 1
        assert stmt["usage"]["ai"]["total_tokens"] == 2000

    def test_set_price_and_recompute(self, api_client):
        client, org_id, _ = api_client
        _ok(client.post(f"/api/v1/enterprise/{org_id}/billing/prices/seed"))
        p = _ok(client.put(f"/api/v1/enterprise/{org_id}/billing/prices",
                           json={"item": "ai_token_1k",
                                 "unit_price_cents": 3,
                                 "note": "测试价"}))
        assert p["version"] == 2  # 只增版本
        _seed_ai_call(org_id, tokens=2000, cost=6)
        stmt = _ok(client.get(
            f"/api/v1/enterprise/{org_id}/billing/statement"))
        assert stmt["prices_unset"] is False
        line = next(li for li in stmt["lines"]
                    if li["item"] == "ai_token_1k")
        assert line["amount_cents"] == 6  # 2 千 token × 3 分
        assert stmt["amount_cents"] == 6

    def test_issue_record_idempotent(self, api_client):
        client, org_id, _ = api_client
        _ok(client.post(f"/api/v1/enterprise/{org_id}/billing/prices/seed"))
        rec = client.post(f"/api/v1/enterprise/{org_id}/billing/records",
                          json={"period": "2026-10"})
        assert rec.status_code == 200, rec.text[:200]
        assert _ok(rec)["status"] == "issued"
        # 重复开 → 409
        r = client.post(f"/api/v1/enterprise/{org_id}/billing/records",
                        json={"period": "2026-10"})
        assert r.status_code == 409
        recs = _ok(client.get(f"/api/v1/enterprise/{org_id}/billing/records"))
        assert len(recs) == 1


class TestUsageReport:
    def test_report_aggregates(self, api_client):
        client, org_id, _ = api_client
        _seed_ai_call(org_id, tokens=1000, cost=3, model="m1")
        _seed_ai_call(org_id, tokens=2000, cost=6, model="m2",
                      task_type="storyboard")
        for s in _db():
            s.add(models.Work(org_id=org_id, owner_id="u_owner",
                              title="w", data_json="{}"))
            s.commit()
        rep = _ok(client.get(
            f"/api/v1/enterprise/{org_id}/usage/report"))
        assert rep["ai"]["calls"] == 2
        assert rep["ai"]["total_tokens"] == 3000
        assert rep["ai"]["cost_cents"] == 9
        assert rep["ai"]["by_model"]["m1"]["calls"] == 1
        assert rep["ai"]["by_task_type"]["storyboard"]["calls"] == 1
        assert rep["members"] == 2
        assert rep["tasks_by_status"]["succeeded"] == 2
        assert isinstance(rep["notes"], list) and rep["notes"]

    def test_bad_date(self, api_client):
        client, org_id, _ = api_client
        r = client.get(f"/api/v1/enterprise/{org_id}/usage/report",
                       params={"date_from": "not-a-date"})
        assert r.status_code == 400
