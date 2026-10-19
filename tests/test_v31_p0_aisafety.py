"""V3.1 P0：AI 生成链路安全修复回归测试。

- POST /api/v1/ai-tasks：强制登录 / 组织成员校验（404 防枚举）/ Idempotency-Key 双击只建一个任务
- GET /api/v1/ai-tasks/{id}：跨组织 404 租户隔离
- POST /api/colloquial-rewrite：强制登录 / 冒用他人组织头 404
- _validate_livestream：phase 枚举 / 时间连续无重叠无空洞 / 覆盖 0→total / 时长一致 / script 非空
- _gateway_for_runner：org_id 透传进网关（预算生效）
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/v31p0.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.core.idempotency import reset_idempotency_store
    from backend.security.rate_limit import reset_rate_limit_buckets
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()
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
    for uid in ("u1", "u2"):
        db.add(AuthUser(id=uid, email=f"{uid}@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org1.id, role="owner"))
    db.add(Membership(user_id="u2", org_id=org2.id, role="owner"))
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

        def anon(self):
            app.dependency_overrides.pop(get_current_user, None)
            return client

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()


def _task_body(org_id):
    return {"task_type": "livestream_script", "org_id": org_id,
            "payload": {"product": {"product_name": "樱桃"},
                        "duration_minutes": 30}}


def _count_tasks(org_id):
    from backend.auth.database import get_session_factory
    from backend.ai_tasks.repository import AITaskRepository
    s = get_session_factory()()
    try:
        return len(AITaskRepository(s).list_by_org(org_id))
    finally:
        s.close()


def _seed_task(org_id, task_type="livestream_script"):
    from backend.auth.database import get_session_factory
    from backend.ai_tasks.repository import AITaskRepository
    s = get_session_factory()()
    try:
        t = AITaskRepository(s).create(org_id=org_id, task_type=task_type,
                                      payload={}, created_by="u9")
        s.commit()
        return t.id
    finally:
        s.close()


class FakeGateway:
    """最小网关替身：只验证鉴权/租户链路，不走到模型调用。"""

    def generate_with_retry(self, *a, **k):
        raise AssertionError("测试不应走到模型调用")


class TestCreateTaskAuth:
    def test_requires_login(self, api_env):
        r = api_env.anon().post("/api/v1/ai-tasks",
                                json=_task_body(api_env.org1))
        assert r.status_code == 401

    def test_rejects_foreign_org(self, api_env):
        r = api_env.as_user("u1").post(
            "/api/v1/ai-tasks", json=_task_body(api_env.org2))
        assert r.status_code == 404  # 防枚举：与"组织不存在"同口径

    def test_missing_org_404(self, api_env):
        body = _task_body(None)
        r = api_env.as_user("u1").post("/api/v1/ai-tasks", json=body)
        assert r.status_code == 404

    def test_idempotent_double_submit(self, api_env):
        c = api_env.as_user("u1")
        h = {"Idempotency-Key": "v31-p0-double-1"}
        r1 = c.post("/api/v1/ai-tasks", json=_task_body(api_env.org1),
                     headers=h)
        r2 = c.post("/api/v1/ai-tasks", json=_task_body(api_env.org1),
                     headers=h)
        assert r1.status_code == 202 and r2.status_code == 202
        assert r1.json()["id"] == r2.json()["id"]  # 双击只建一个任务
        assert r2.json().get("idempotent_replay") is True
        assert _count_tasks(api_env.org1) == 1

    def test_created_by_is_server_user(self, api_env):
        c = api_env.as_user("u1")
        body = _task_body(api_env.org1)
        body["created_by"] = "attacker"
        r = c.post("/api/v1/ai-tasks", json=body,
                   headers={"Idempotency-Key": "v31-p0-cb-1"})
        assert r.status_code == 202
        from backend.auth.database import get_session_factory
        from backend.ai_tasks.repository import AITaskRepository
        s = get_session_factory()()
        try:
            t = AITaskRepository(s).get(r.json()["id"])
            assert t.created_by == "u1"  # 不信任客户端传入
        finally:
            s.close()


class TestGetTaskIsolation:
    def test_requires_login(self, api_env):
        tid = _seed_task(api_env.org1)
        r = api_env.anon().get(f"/api/v1/ai-tasks/{tid}")
        assert r.status_code == 401

    def test_cross_org_404(self, api_env):
        tid = _seed_task(api_env.org2)
        r = api_env.as_user("u1").get(f"/api/v1/ai-tasks/{tid}")
        assert r.status_code == 404

    def test_own_org_ok(self, api_env):
        tid = _seed_task(api_env.org1)
        r = api_env.as_user("u1").get(f"/api/v1/ai-tasks/{tid}")
        assert r.status_code == 200
        assert r.json()["id"] == tid

    def test_cancel_cross_org_404(self, api_env):
        tid = _seed_task(api_env.org2)
        r = api_env.as_user("u1").post(f"/api/v1/ai-tasks/{tid}/cancel")
        assert r.status_code == 404

    def test_list_cross_org_404(self, api_env):
        r = api_env.as_user("u1").get(
            "/api/v1/ai-tasks", params={"org_id": api_env.org2})
        assert r.status_code == 404


class TestColloquialAuth:
    BODY = {"text": "这款苹果口感脆甜，欢迎大家购买。",
            "product_name": "烟台苹果"}

    def test_requires_login(self, api_env):
        r = api_env.anon().post("/api/colloquial-rewrite", json=self.BODY)
        assert r.status_code == 401

    def test_rejects_foreign_org_header(self, api_env):
        # 冒用他人组织的 X-Org-Id 刷预算 → 404（网关依赖先 mock，避免无 AI 配置 503）
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        app.dependency_overrides[get_ai_gateway] = lambda: FakeGateway()
        try:
            r = api_env.as_user("u1").post(
                "/api/colloquial-rewrite", json=self.BODY,
                headers={"X-Org-Id": api_env.org2})
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_ai_gateway, None)

    def test_ok_with_login_and_fake_gw(self, api_env):
        import threading
        from backend.app import app
        from backend.core.deps import get_ai_gateway

        class FakeGW:
            def generate_with_retry(self, task_type, make_messages,
                                    validate_fn, **kw):
                data = {"rewritten": "改好了", "changes": ["更口语"]}
                errs, out = validate_fn(data)
                assert not errs
                return out, {"attempts": 1}

        app.dependency_overrides[get_ai_gateway] = lambda: FakeGW()
        try:
            r = api_env.as_user("u1").post(
                "/api/colloquial-rewrite", json=self.BODY,
                headers={"X-Org-Id": api_env.org1})
            assert r.status_code == 200
            assert r.json()["rewritten"] == "改好了"
        finally:
            app.dependency_overrides.pop(get_ai_gateway, None)


def _seg(phase, st, en, script=None):
    return {"phase": phase, "start_minute": st, "end_minute": en,
            "script": script if script is not None else ["话术一句"]}


def _full_script(total=30, duration=None):
    return {"title": "专场", "total_minutes": total,
            "segments": [_seg("开场留人", 0, 5),
                         _seg("讲品", 5, 20),
                         _seg("逼单", 20, total)],
            "lucky_bag_points": [], "closing_scripts": ["抓紧下单"],
            "rescue_scripts": [], "prep_checklist": []}


class TestLivestreamValidator:
    def test_valid_full_timeline(self):
        from backend.ai_tasks.runners import _validate_livestream
        errs, out = _validate_livestream(_full_script(30), duration=30)
        assert errs == [] and out is not None

    def test_bad_phase(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script()
        d["segments"][1]["phase"] = "乱讲"
        errs, out = _validate_livestream(d)
        assert out is None and any("phase 非法" in e for e in errs)

    def test_gap(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script()
        d["segments"][1]["start_minute"] = 6  # 5→6 空洞
        errs, out = _validate_livestream(d)
        assert out is None and any("不连续" in e for e in errs)

    def test_overlap(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script()
        d["segments"][2]["start_minute"] = 18  # 与上一段重叠
        errs, out = _validate_livestream(d)
        assert out is None and any("不连续" in e for e in errs)

    def test_not_covering_total(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script(30)
        d["segments"][-1]["end_minute"] = 25  # 没覆盖到 30
        errs, out = _validate_livestream(d)
        assert out is None and any("未完整覆盖" in e for e in errs)

    def test_total_mismatch(self):
        from backend.ai_tasks.runners import _validate_livestream
        errs, out = _validate_livestream(_full_script(30), duration=60)
        assert out is None and any("不一致" in e for e in errs)

    def test_empty_script(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script()
        d["segments"][0]["script"] = []
        errs, out = _validate_livestream(d)
        assert out is None and any("script 为空" in e for e in errs)

    def test_start_not_zero(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _full_script()
        d["segments"][0]["start_minute"] = 1
        errs, out = _validate_livestream(d)
        assert out is None and any("不连续" in e for e in errs)


class TestRunnerGateway:
    def test_gateway_carries_org(self):
        from backend.ai_tasks.runners import _gateway_for_runner
        gw, db = _gateway_for_runner("t1", {"_org_id": "orgX"},
                                     service=object())
        try:
            assert gw._org_id == "orgX"  # 预算生效的前提
        finally:
            db.close()

    def test_gateway_no_org_backward_compat(self):
        from backend.ai_tasks.runners import _gateway_for_runner
        gw, db = _gateway_for_runner("t1", {}, service=object())
        try:
            assert gw._org_id is None  # 旧 payload 照常工作
        finally:
            db.close()
