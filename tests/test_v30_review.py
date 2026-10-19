"""V3.0 H：效果复盘测试。

- 已发布→已复盘流转（复用发布中心 transition，跳步被拒）
- 指标录入：只有已发布记录可录入、非法数据被拒、多次追加历史可查
- 复盘笔记、AI 建议（无 key 503 诚实失败；建议只存建议表不覆盖已确认内容）
- 效果看板：空数据空状态，无假图表
- 权限隔离、错误码
"""
import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/review.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import (
        reset_engine, get_session_factory, get_db,
    )
    from backend.auth.deps import get_current_user
    from backend.security.rate_limit import reset_rate_limit_buckets
    from backend.core.idempotency import reset_idempotency_store
    reset_engine()
    reset_rate_limit_buckets()
    reset_idempotency_store()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org1 = Organization(name="合作社A", created_by="u1")
    org2 = Organization(name="合作社B", created_by="u2")
    db.add(org1)
    db.add(org2)
    db.flush()
    for uid in ("u1", "u2", "u_viewer"):
        db.add(AuthUser(id=uid, email=f"{uid}@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org1.id, role="owner"))
    db.add(Membership(user_id="u2", org_id=org2.id, role="owner"))
    db.add(Membership(user_id="u_viewer", org_id=org1.id, role="viewer"))
    db.commit()
    ids = (org1.id, org2.id)
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app
    app.dependency_overrides.clear()

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)

    class Env:
        org1, org2 = ids

        def as_user(self, uid):
            def _u():
                return AuthUser(id=uid, email=f"{uid}@test.local",
                                password_hash="x")
            app.dependency_overrides[get_current_user] = _u
            return client

        def db(self):
            return get_session_factory()()

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_rate_limit_buckets()
    reset_idempotency_store()


def _pending_record(client, org_id, title="樱桃发布"):
    r = client.post("/api/v1/publish/records", params={"org_id": org_id},
                    json={"title": title, "platform": "douyin"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _published_record(env, org_id, title="樱桃发布"):
    """直插一条已发布记录（绕开冗长的发布前检查链路）。"""
    from backend import models
    db = env.db()
    rec = models.PublishRecord(
        org_id=org_id, title=title, platform="douyin",
        status=models.PUBLISH_PUBLISHED,
        published_at=datetime.now(timezone.utc),
        publish_url="https://www.douyin.com/video/123",
        created_by="u1")
    db.add(rec)
    db.commit()
    rid = rec.id
    db.close()
    return rid


def _metric_payload(**kw):
    base = {"source": "manual", "date_start": "2026-10-10",
            "date_end": "2026-10-16",
            "metrics": {"plays": 1200, "likes": 86, "comments": 12},
            "note": "首周数据"}
    base.update(kw)
    return base


class FakeReviewGateway:
    """假网关：返回固定建议；统计调用次数。"""

    def __init__(self):
        self.calls = 0

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            **kwargs):
        self.calls += 1
        msgs = make_messages(None)
        assert isinstance(msgs, list) and len(msgs) == 2
        data = {"observations": ["播放量 1200，点赞 86"],
                "suggestions": ["把前 3 秒改成价格直给（推测）"]}
        out = validate_fn(data)
        return out, {"attempts": 1, "model": "fake"}


# ---------- 状态流转 ----------

def test_published_to_reviewed_via_transition(api_env):
    """已发布 → 已复盘：复用发布中心 transition 接口。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.post(f"/api/v1/publish/records/{rid}/transition",
                    params={"org_id": env.org1},
                    json={"to_status": "reviewed"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "reviewed"


def test_skip_step_to_reviewed_rejected(api_env):
    """pending → reviewed 跳步被拒（码 3015）。"""
    env = api_env
    client = env.as_user("u1")
    rid = _pending_record(client, env.org1)
    r = client.post(f"/api/v1/publish/records/{rid}/transition",
                    params={"org_id": env.org1},
                    json={"to_status": "reviewed"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == 3015


# ---------- 指标录入 ----------

def test_metric_create_and_history(api_env):
    """多次追加 + 历史按录入顺序可查 + has_data。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    for i, plays in enumerate((1200, 3500)):
        p = _metric_payload(metrics={"plays": plays, "likes": 10 + i})
        r = client.post(f"/api/v1/review/records/{rid}/metrics",
                        params={"org_id": env.org1}, json=p)
        assert r.status_code == 201, r.text
        assert r.json()["data"]["metric"]["source_label"] == "人工录入"
    r = client.get(f"/api/v1/review/records/{rid}/metrics",
                   params={"org_id": env.org1})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_data"] is True
    assert [m["metrics"]["plays"] for m in data["metrics"]] == [1200, 3500]


def test_metric_rejected_when_not_published(api_env):
    """未发布记录录入指标被拒（码 3023）。"""
    env = api_env
    client = env.as_user("u1")
    rid = _pending_record(client, env.org1)
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1}, json=_metric_payload())
    assert r.status_code == 400, r.text
    assert r.json()["code"] == 3023


def test_metric_invalid_value_rejected(api_env):
    """指标值非数字/负数被拒（码 3020）。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1},
                    json=_metric_payload(metrics={"plays": "很多"}))
    assert r.status_code == 400 and r.json()["code"] == 3020, r.text
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1},
                    json=_metric_payload(metrics={"plays": -5}))
    assert r.status_code == 400 and r.json()["code"] == 3020, r.text


def test_metric_invalid_date_range_rejected(api_env):
    """开始日期晚于结束日期被拒（码 3020）。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1},
                    json=_metric_payload(date_start="2026-10-16",
                                         date_end="2026-10-10"))
    assert r.status_code == 400 and r.json()["code"] == 3020, r.text


def test_metric_invalid_source_rejected(api_env):
    """来源非法被拒（码 3020）；截图来源合法。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1},
                    json=_metric_payload(source="guess"))
    assert r.status_code == 400 and r.json()["code"] == 3020, r.text
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1},
                    json=_metric_payload(source="screenshot"))
    assert r.status_code == 201, r.text
    assert r.json()["data"]["metric"]["source_label"] == "平台截图导入"


# ---------- 复盘笔记与总览 ----------

def test_review_note_roundtrip(api_env):
    """复盘笔记保存后可读；总览聚合笔记+指标+建议。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.patch(f"/api/v1/review/records/{rid}/review-note",
                     params={"org_id": env.org1},
                     json={"review_note": "前3秒留人弱，下次改价格直给。"})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/review/records/{rid}/review",
                   params={"org_id": env.org1})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["review_note"] == "前3秒留人弱，下次改价格直给。"
    assert data["has_data"] is False
    assert data["metrics"] == [] and data["suggestions"] == []


# ---------- 效果看板 ----------

def test_dashboard_empty_state(api_env):
    """无数据 → has_data=false + 空状态提示，不造假图表。"""
    env = api_env
    client = env.as_user("u1")
    _published_record(env, env.org1)
    r = client.get("/api/v1/review/dashboard", params={"org_id": env.org1})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_data"] is False
    assert "暂无数据" in data["empty_hint"]
    assert all(rec["batches"] == 0 for rec in data["records"])


def test_dashboard_with_data(api_env):
    """有数据 → 列表 + 最新一批 + 完整历史。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1, title="樱桃发布A")
    for plays in (1000, 2500):
        r = client.post(f"/api/v1/review/records/{rid}/metrics",
                        params={"org_id": env.org1},
                        json=_metric_payload(metrics={"plays": plays}))
        assert r.status_code == 201, r.text
    r = client.get("/api/v1/review/dashboard", params={"org_id": env.org1})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_data"] is True
    item = next(i for i in data["records"] if i["record_id"] == rid)
    assert item["batches"] == 2
    assert item["latest"]["metrics"]["plays"] == 2500
    assert len(item["history"]) == 2


# ---------- AI 建议 ----------

def test_suggest_no_key_503(api_env):
    """无 AI 配置时诚实 503，不伪造建议。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    r = client.post(f"/api/v1/review/records/{rid}/suggest",
                    params={"org_id": env.org1})
    assert r.status_code == 503, r.text


def test_suggest_stores_only_no_overwrite(api_env):
    """建议只存建议表：发布记录字段、指标均不被触碰。"""
    env = api_env
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakeReviewGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    client = env.as_user("u1")
    rid = _published_record(env, env.org1, title="樱桃发布B")
    client.post(f"/api/v1/review/records/{rid}/metrics",
                params={"org_id": env.org1}, json=_metric_payload())
    r = client.post(f"/api/v1/review/records/{rid}/suggest",
                    params={"org_id": env.org1})
    assert r.status_code == 200, r.text
    sug = r.json()["data"]["suggestion"]
    assert sug["status"] == "pending"
    assert "直接观察" in sug["suggestion_text"]

    from backend import models
    db = env.db()
    rec = db.query(models.PublishRecord).filter(
        models.PublishRecord.id == rid).one()
    # 发布记录原样：标题/链接/笔记都没被 AI 动过
    assert rec.title == "樱桃发布B"
    assert rec.review_note == ""
    assert rec.status == "published"
    assert db.query(models.EffectMetric).filter(
        models.EffectMetric.publish_record_id == rid).count() == 1
    assert db.query(models.ReviewSuggestion).filter(
        models.ReviewSuggestion.publish_record_id == rid).count() == 1
    db.close()


def test_suggestion_decision_only_changes_status(api_env):
    """采纳/忽略只改状态，不写回任何业务数据。"""
    env = api_env
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    app.dependency_overrides[get_ai_gateway] = lambda: FakeReviewGateway()
    client = env.as_user("u1")
    rid = _published_record(env, env.org1, title="樱桃发布C")
    r = client.post(f"/api/v1/review/records/{rid}/suggest",
                    params={"org_id": env.org1})
    sug_id = r.json()["data"]["suggestion"]["id"]
    text_before = r.json()["data"]["suggestion"]["suggestion_text"]

    r = client.post(f"/api/v1/review/suggestions/{sug_id}/decision",
                    params={"org_id": env.org1},
                    json={"decision": "adopted"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["suggestion"]["status"] == "adopted"
    assert r.json()["data"]["suggestion"]["suggestion_text"] == text_before
    # 发布记录仍是 published，没有被"采纳"偷偷推进
    from backend import models
    db = env.db()
    rec = db.query(models.PublishRecord).filter(
        models.PublishRecord.id == rid).one()
    assert rec.status == "published"
    db.close()


def test_suggest_idempotent_replay(api_env):
    """同幂等键第二次返回回放，网关只调一次。"""
    env = api_env
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakeReviewGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    client = env.as_user("u1")
    rid = _published_record(env, env.org1, title="樱桃发布D")
    headers = {"Idempotency-Key": "review-suggest-key-1"}
    r1 = client.post(f"/api/v1/review/records/{rid}/suggest",
                     params={"org_id": env.org1}, headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/v1/review/records/{rid}/suggest",
                     params={"org_id": env.org1}, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("idempotent_replay") is True
    assert fake.calls == 1


# ---------- 权限 ----------

def test_cross_org_isolation(api_env):
    """跨组织读指标 404（防枚举）。"""
    env = api_env
    client = env.as_user("u1")
    rid = _published_record(env, env.org1)
    client2 = env.as_user("u2")
    r = client2.get(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org2})
    assert r.status_code == 404, r.text


def test_viewer_cannot_record_metric(api_env):
    """viewer 无复盘写权限（404）。"""
    env = api_env
    env.as_user("u1")
    rid = _published_record(env, env.org1)
    client = env.as_user("u_viewer")
    r = client.post(f"/api/v1/review/records/{rid}/metrics",
                    params={"org_id": env.org1}, json=_metric_payload())
    assert r.status_code == 404, r.text
