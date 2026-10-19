"""V3.0 C：内容策划工作台测试。

- CRUD / 非法目标 400 / 跨组织隔离 / 无权 404（防枚举）
- 4 种内容目标 prompt 逻辑不同（单元）
- 生成：mock 网关成功 / AI 失败 503 无残留 / 候选数量校验
- 选择：无候选 400 / 越界 400 / 正常选中
- 可行性：未选中 400 / 选中后纯规则返回
- 关联拍摄项目：未选中 400 / 关联已有（direction 写入）/ 新建项目
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
    """两组织环境：org1(owner u1 + viewer u_viewer)、org2(owner u2)。"""
    url = f"sqlite:///{tmp_path}/plan.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import (
        reset_engine, get_session_factory, get_db,
    )
    from backend.auth.deps import get_current_user
    reset_engine()
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
        db.add(AuthUser(id=uid, email=f"{uid}@test.local",
                        password_hash="x"))
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

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    from backend.core.idempotency import reset_idempotency_store
    from backend.security.rate_limit import reset_rate_limit_buckets
    reset_idempotency_store()
    reset_rate_limit_buckets()


class FakeGateway:
    """假网关：返回固定 4 个候选；可抛错模拟失败链路。"""

    def __init__(self, fail_with=None, n_candidates=4):
        self.calls = 0
        self.fail_with = fail_with
        self.n_candidates = n_candidates

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            **kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with("MOCK: AI 不可用")
        cands = []
        for i in range(self.n_candidates):
            cands.append({
                "name": f"方向{i + 1}",
                "goal": "brand",
                "core_expression": f"核心表达{i + 1}",
                "hook_opening": f"开头{i + 1}",
                "structure": "开头-中间-结尾",
                "suggested_shots": f"画面{i + 1}",
                "difficulty": "低",
                "shoot_notes": "注意光线",
                "fact_refs": [],
            })
        errs, out = validate_fn({"candidates": cands})
        assert not errs, errs
        return out, {"attempts": 1}


@pytest.fixture()
def fake_gw():
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakeGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    return fake


TASK_BODY = {
    "title": "樱桃品牌宣传策划",
    "content_goal": "brand",
    "audience": "25-40 岁城市女性",
    "content_purpose": "提升品牌认知",
    "core_message": "果园来信，当季鲜",
    "selling_points": ["果大", "汁多"],
    "style": "实在不端着",
    "target_duration_seconds": 30,
    "target_platforms": ["抖音", "快手"],
    "shoot_conditions": {"device": "手机", "people": 1,
                         "lighting": "自然光", "location": "果园"},
    "ad_restrictions": "不宣称医疗功效",
}


def _create(client, org_id, **kw):
    body = dict(TASK_BODY)
    body.update(kw)
    return client.post("/api/v1/plan/tasks",
                       params={"org_id": org_id}, json=body)


def _gen(client, org_id, task_id):
    return client.post(f"/api/v1/plan/tasks/{task_id}/generate",
                       params={"org_id": org_id})


class TestPlanCRUD:
    def test_create_and_get(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        assert r.status_code == 201, r.text
        data = r.json()["data"]
        assert data["content_goal"] == "brand"
        assert data["status"] == "draft"
        tid = data["id"]
        r = client.get(f"/api/v1/plan/tasks/{tid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200
        assert r.json()["data"]["title"] == "樱桃品牌宣传策划"

    def test_create_invalid_goal(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1, content_goal="nope")
        assert r.status_code == 400, r.text

    def test_list_and_delete(self, api_env):
        client = api_env.as_user("u1")
        _create(client, api_env.org1, title="A")
        _create(client, api_env.org1, title="B", content_goal="sales")
        r = client.get("/api/v1/plan/tasks", params={"org_id": api_env.org1})
        assert len(r.json()["data"]) == 2
        r = client.get("/api/v1/plan/tasks",
                       params={"org_id": api_env.org1, "goal": "sales"})
        assert len(r.json()["data"]) == 1
        tid = r.json()["data"][0]["id"]
        r = client.delete(f"/api/v1/plan/tasks/{tid}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 204
        r = client.get(f"/api/v1/plan/tasks/{tid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 404

    def test_cross_org_isolation(self, api_env):
        c1 = api_env.as_user("u1")
        tid = _create(c1, api_env.org1).json()["data"]["id"]
        c2 = api_env.as_user("u2")
        r = c2.get(f"/api/v1/plan/tasks/{tid}",
                   params={"org_id": api_env.org2})
        assert r.status_code == 404, r.text

    def test_viewer_cannot_create(self, api_env):
        client = api_env.as_user("u_viewer")
        r = _create(client, api_env.org1)
        assert r.status_code == 404, r.text

    def test_update_settings(self, api_env):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        r = client.patch(f"/api/v1/plan/tasks/{tid}",
                         params={"org_id": api_env.org1},
                         json={"audience": "全年龄", "content_goal": "sales"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["audience"] == "全年龄"
        assert r.json()["data"]["content_goal"] == "sales"
        r = client.patch(f"/api/v1/plan/tasks/{tid}",
                         params={"org_id": api_env.org1},
                         json={"content_goal": "nope"})
        assert r.status_code == 400


class TestPlanPrompt:
    def test_four_goals_different_logic(self):
        from backend.ai.prompts import build_plan_messages
        texts = {}
        for goal in ("brand", "sales", "intro", "record"):
            msgs = build_plan_messages({"content_goal": goal}, [])
            texts[goal] = msgs[0]["content"]
        # 4 种目标各有不同的生成逻辑文本
        assert "品牌识别" in texts["brand"]
        assert "为什么现在买" in texts["sales"]
        assert "已确认的产品特征" in texts["intro"]
        assert "真实场景" in texts["record"]
        # 互不相同
        assert len(set(texts.values())) == 4

    def test_validate_candidates_count(self):
        from backend.api.v1.plan import _validate_plan
        good = {"candidates": [
            {"name": f"n{i}", "goal": "brand", "core_expression": "c",
             "hook_opening": "h", "structure": "s", "suggested_shots": "v",
             "difficulty": "低"} for i in range(3)]}
        errs, out = _validate_plan(good)
        assert not errs and len(out) == 3
        errs, _ = _validate_plan({"candidates": good["candidates"][:2]})
        assert errs  # 少于 3 个
        errs, _ = _validate_plan({"candidates": good["candidates"] * 2})
        assert errs  # 6 个超上限
        errs, _ = _validate_plan({})
        assert errs  # 缺数组


class TestPlanGenerate:
    def test_generate_success(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        r = _gen(client, api_env.org1, tid)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert len(data["candidates"]) == 4
        assert data["status"] == "planned"
        c = data["candidates"][0]
        for k in ("name", "core_expression", "hook_opening", "structure",
                  "suggested_shots", "difficulty"):
            assert c[k], k
        assert "review_reminder" in r.json()

    def test_generate_ai_failure_503_no_candidates(self, api_env):
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        from backend.ai_service import AIConfigError
        app.dependency_overrides[get_ai_gateway] = lambda: FakeGateway(
            fail_with=AIConfigError)
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        r = _gen(client, api_env.org1, tid)
        assert r.status_code == 503, r.text
        r = client.get(f"/api/v1/plan/tasks/{tid}",
                       params={"org_id": api_env.org1})
        assert r.json()["data"]["candidates"] == []
        assert r.json()["data"]["status"] == "draft"


class TestPlanSelect:
    def _gen_task(self, client, org_id, fake_gw):
        tid = _create(client, org_id).json()["data"]["id"]
        r = _gen(client, org_id, tid)
        assert r.status_code == 200, r.text
        return tid

    def test_select_without_candidates(self, api_env):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/plan/tasks/{tid}/select",
                        params={"org_id": api_env.org1}, json={"index": 0})
        assert r.status_code == 400, r.text

    def test_select_out_of_range(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = self._gen_task(client, api_env.org1, fake_gw)
        r = client.post(f"/api/v1/plan/tasks/{tid}/select",
                        params={"org_id": api_env.org1}, json={"index": 9})
        assert r.status_code == 400, r.text

    def test_select_valid(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = self._gen_task(client, api_env.org1, fake_gw)
        r = client.post(f"/api/v1/plan/tasks/{tid}/select",
                        params={"org_id": api_env.org1}, json={"index": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["selected_index"] == 1
        assert data["selected_idea"]["name"] == "方向2"


class TestPlanFeasibility:
    def test_feasibility_without_selection(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        _gen(client, api_env.org1, tid)
        r = client.post(f"/api/v1/plan/tasks/{tid}/feasibility",
                        params={"org_id": api_env.org1})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == 3011

    def test_feasibility_with_selection(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        _gen(client, api_env.org1, tid)
        client.post(f"/api/v1/plan/tasks/{tid}/select",
                    params={"org_id": api_env.org1}, json={"index": 0})
        r = client.post(f"/api/v1/plan/tasks/{tid}/feasibility",
                        params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["status"] in ("ready", "need_confirm", "need_alternative")
        assert isinstance(data.get("checks"), list)


class TestPlanLinkWork:
    def _gen_and_select(self, client, org_id, fake_gw):
        tid = _create(client, org_id).json()["data"]["id"]
        _gen(client, org_id, tid)
        client.post(f"/api/v1/plan/tasks/{tid}/select",
                    params={"org_id": org_id}, json={"index": 0})
        return tid

    def test_link_without_selection(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = _create(client, api_env.org1).json()["data"]["id"]
        _gen(client, api_env.org1, tid)
        r = client.post(f"/api/v1/plan/tasks/{tid}/link-work",
                        params={"org_id": api_env.org1},
                        json={"work_id": "whatever"})
        assert r.status_code == 400, r.text

    def test_link_existing_work(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = self._gen_and_select(client, api_env.org1, fake_gw)
        # 先建一个拍摄项目
        wr = client.post("/api/v1/works",
                         json={"org_id": api_env.org1, "title": "樱桃拍摄",
                               "data": {}})
        assert wr.status_code in (200, 201), wr.text
        wid = wr.json()["data"]["id"]
        r = client.post(f"/api/v1/plan/tasks/{tid}/link-work",
                        params={"org_id": api_env.org1},
                        json={"work_id": wid})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["work_id"] == wid
        assert data["status"] == "linked"
        # direction 被写入 work 的 data_json，其他内容不动
        wr2 = client.get(f"/api/v1/works/{wid}",
                         params={"org_id": api_env.org1})
        wdata = wr2.json()["data"]["data"]
        assert "方向1" in wdata["direction"]
        assert wdata["plan_task_id"] == tid

    def test_link_create_new_work(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = self._gen_and_select(client, api_env.org1, fake_gw)
        r = client.post(f"/api/v1/plan/tasks/{tid}/link-work",
                        params={"org_id": api_env.org1},
                        json={"create_new": True, "title": "新拍摄项目"})
        assert r.status_code == 200, r.text
        wid = r.json()["data"]["work_id"]
        assert wid
        wr = client.get(f"/api/v1/works/{wid}",
                        params={"org_id": api_env.org1})
        assert wr.status_code == 200
        assert "方向1" in wr.json()["data"]["data"]["direction"]

    def test_link_nonexistent_work(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        tid = self._gen_and_select(client, api_env.org1, fake_gw)
        r = client.post(f"/api/v1/plan/tasks/{tid}/link-work",
                        params={"org_id": api_env.org1},
                        json={"work_id": "no-such-work"})
        assert r.status_code == 404, r.text
