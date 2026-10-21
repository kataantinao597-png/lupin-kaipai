"""补充路由测试：POST /api/v1/agent/one-shot 与 POST /api/v1/agent/modify。

覆盖：
- 未登录 401；非成员组织 404（防枚举）
- one-shot：202 建 run + 入队（monkeypatch submit 内联执行，
  fake run_one_shot_brief，不调真实模型）
- one-shot：Idempotency-Key 同 key 同体回放（不重复建 run）
- one-shot：work 跨组织 404；brief_text 为空 422
- modify：200 同步返回（monkeypatch apply_modification）
- modify：跨作品/会话不存在 404；解析失败 400

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。
不调真实模型。

运行：./venv/bin/python -m pytest tests/test_agent_http_supplement.py
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """每用例一个 scratch 库 + TestClient；app 在 env 就绪后导入。"""
    import queue as _queue_mod
    import time as _time
    from backend import ai_observability as _obs
    monkeypatch.setattr(_obs, "record_call_async", lambda rec: None)
    for _ in range(20):
        drained = 0
        while True:
            try:
                _obs._record_q.get_nowait()
                drained += 1
            except _queue_mod.Empty:
                break
        _time.sleep(0.2)
        try:
            _obs._record_q.get_nowait()
        except _queue_mod.Empty:
            if drained == 0:
                break

    url = f"sqlite:///{tmp_path}/agent_http_sup.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import (
        get_db, get_session_factory, reset_engine,
    )
    from backend.auth.deps import get_current_user
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Membership, Organization, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org_a = Organization(name="A 合作社", created_by="u_owner")
    org_b = Organization(name="B 合作社", created_by="u_other")
    db.add_all([org_a, org_b])
    db.flush()
    user_a = AuthUser(id="u_owner", email="owner@test.local",
                      password_hash="x")
    user_b = AuthUser(id="u_other", email="other@test.local",
                      password_hash="x")
    db.add_all([user_a, user_b])
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org_a.id, role="owner"))
    db.add(Membership(user_id="u_other", org_id=org_b.id, role="owner"))
    work_a = Work(org_id=org_a.id, owner_id="u_owner", title="A 作品")
    db.add(work_a)
    db.commit()
    # 建一个绑定 work_a 的会话，供 modify 测试用
    from backend.services.agent import create_conversation
    conv_a = create_conversation(db, org_a.id, "u_owner",
                                 work_id=work_a.id)
    db.commit()
    ids = {"org_a": org_a.id, "org_b": org_b.id,
           "work_a": work_a.id, "conv_a": conv_a.id}
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app

    ctx = {"user": user_a}

    def _override_user():
        if ctx["user"] is None:
            raise AssertionError("user override 已移除，不应再被调用")
        return AuthUser(id=ctx["user"].id, email=ctx["user"].email,
                        password_hash="x")

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    from backend.security.rate_limit import reset_rate_limit_buckets
    reset_rate_limit_buckets()
    yield client, ids, ctx
    app.dependency_overrides.clear()
    reset_engine()
    reset_rate_limit_buckets()


def _data(resp):
    assert resp.status_code < 500, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


def _err(resp):
    body = resp.json()
    assert body["code"] != 0, body
    return body


def _patch_submit_inline(monkeypatch):
    """拦截 TaskQueue.submit：内联执行 runner（单线程可测），记录调用。"""
    from backend.ai_tasks.queue import TaskQueue
    calls = []

    def fake_submit(self, task_id, runner, priority="high", **kw):
        calls.append(task_id)
        return runner(task_id, lambda: False)

    monkeypatch.setattr(TaskQueue, "submit", fake_submit)
    return calls


def _fake_one_shot(monkeypatch):
    """fake run_one_shot_brief：直接把 run 置为 succeeded，不调模型。"""
    import backend.services.agent.orchestrator as orch

    def fake(db2, user, org_id, brief_text, task_id=None, **kw):
        from backend.ai_tasks.repository import AITaskRepository
        assert task_id, "路由必须先建 run 再入队"
        repo = AITaskRepository(db2)
        repo.set_status(task_id, "running")
        repo.set_status(task_id, "succeeded", progress=100,
                        result={"run_status": "completed"})
        return {"task_id": task_id, "run_status": "completed"}

    monkeypatch.setattr(orch, "run_one_shot_brief", fake)


class TestOneShot:
    def test_unauthenticated_401(self, api_client):
        from backend.app import app
        from backend.auth.deps import get_current_user
        client, ids, ctx = api_client
        app.dependency_overrides.pop(get_current_user)
        try:
            r = client.post("/api/v1/agent/one-shot",
                            json={"org_id": ids["org_a"],
                                  "brief_text": "卖苹果"})
            assert r.status_code == 401, r.text
            assert r.json()["code"] == 4010, r.text
        finally:
            from backend.auth.models import User as AuthUser
            app.dependency_overrides[get_current_user] = (
                lambda: AuthUser(id="u_owner",
                                 email="owner@test.local",
                                 password_hash="x"))

    def test_nonmember_org_404(self, api_client):
        client, ids, ctx = api_client
        r = client.post("/api/v1/agent/one-shot",
                        json={"org_id": ids["org_b"],
                              "brief_text": "卖苹果"})
        assert r.status_code == 404, r.text

    def test_brief_text_empty_422(self, api_client):
        client, ids, ctx = api_client
        r = client.post("/api/v1/agent/one-shot",
                        json={"org_id": ids["org_a"], "brief_text": ""})
        assert r.status_code == 422, r.text

    def test_work_cross_org_404(self, api_client):
        """work_id 属于别的组织 → 404（防枚举）。"""
        client, ids, ctx = api_client
        from backend.auth.database import get_session_factory
        from backend.models import Work
        db = get_session_factory()()
        work_b = Work(org_id=ids["org_b"], owner_id="u_other",
                      title="B 作品")
        db.add(work_b)
        db.commit()
        wid = work_b.id
        db.close()
        r = client.post("/api/v1/agent/one-shot",
                        json={"org_id": ids["org_a"],
                              "brief_text": "卖苹果", "work_id": wid})
        assert r.status_code == 404, r.text

    def test_one_shot_202_and_enqueued(self, api_client, monkeypatch):
        """202 建 run + 入队执行；轮询能看到 succeeded。"""
        client, ids, ctx = api_client
        calls = _patch_submit_inline(monkeypatch)
        _fake_one_shot(monkeypatch)
        r = client.post("/api/v1/agent/one-shot",
                        json={"org_id": ids["org_a"],
                              "brief_text": "给烟台苹果做条 30 秒口播"})
        assert r.status_code == 202, r.text
        data = _data(r)
        task_id = data["task_id"]
        assert calls == [task_id], "必须且只入队一次"
        # 轮询真实状态
        r2 = client.get(f"/api/v1/agent/runs/{task_id}")
        d2 = _data(r2)
        assert d2["status"] == "succeeded", d2

    def test_one_shot_idempotent_replay(self, api_client, monkeypatch):
        """同 Idempotency-Key + 同体 → 回放，不重复建 run。"""
        client, ids, ctx = api_client
        calls = _patch_submit_inline(monkeypatch)
        _fake_one_shot(monkeypatch)
        from backend.auth.database import get_session_factory
        from backend.models import AITask
        body = {"org_id": ids["org_a"], "brief_text": "卖苹果"}
        h = {"Idempotency-Key": "one-shot-replay-001"}
        r1 = client.post("/api/v1/agent/one-shot", json=body, headers=h)
        r2 = client.post("/api/v1/agent/one-shot", json=body, headers=h)
        assert r1.status_code == 202 and r2.status_code == 202
        assert _data(r1)["task_id"] == _data(r2)["task_id"]
        assert len(calls) == 1, "回放不应再次入队执行"
        db = get_session_factory()()
        n = db.query(AITask).filter(
            AITask.task_type == "agent_run").count()
        db.close()
        assert n == 1, "只应建一个 run"


class TestModify:
    def _patch_apply(self, monkeypatch, result=None, exc=None):
        import backend.services.agent.modify as mod

        def fake(db, user, org_id, work_id, conversation_id, user_text,
                 **kw):
            if exc is not None:
                raise exc
            return result

        monkeypatch.setattr(mod, "apply_modification", fake)

    def test_modify_200_completed(self, api_client, monkeypatch):
        client, ids, ctx = api_client
        self._patch_apply(monkeypatch, result={
            "status": "completed", "summary": "已压缩到 30 秒"})
        r = client.post("/api/v1/agent/modify", json={
            "org_id": ids["org_a"], "work_id": ids["work_a"],
            "conversation_id": ids["conv_a"],
            "user_text": "口播压缩到 30 秒"})
        assert r.status_code == 200, r.text
        assert _data(r)["status"] == "completed"

    def test_modify_waiting_user_passthrough(self, api_client, monkeypatch):
        """waiting_user 原样透出（前端走确认流）。"""
        client, ids, ctx = api_client
        self._patch_apply(monkeypatch, result={
            "status": "waiting_user", "task_id": "t1",
            "confirm_reason": "锁定镜头需确认"})
        r = client.post("/api/v1/agent/modify", json={
            "org_id": ids["org_a"], "work_id": ids["work_a"],
            "conversation_id": ids["conv_a"], "user_text": "重做第 3 镜"})
        assert _data(r)["status"] == "waiting_user"

    def test_modify_unauthenticated_401(self, api_client):
        from backend.app import app
        from backend.auth.deps import get_current_user
        client, ids, ctx = api_client
        app.dependency_overrides.pop(get_current_user)
        try:
            r = client.post("/api/v1/agent/modify", json={
                "org_id": ids["org_a"], "work_id": ids["work_a"],
                "conversation_id": ids["conv_a"], "user_text": "x"})
            assert r.status_code == 401, r.text
        finally:
            from backend.auth.models import User as AuthUser
            app.dependency_overrides[get_current_user] = (
                lambda: AuthUser(id="u_owner",
                                 email="owner@test.local",
                                 password_hash="x"))

    def test_modify_nonmember_org_404(self, api_client, monkeypatch):
        client, ids, ctx = api_client
        self._patch_apply(monkeypatch,
                          result={"status": "completed"})
        r = client.post("/api/v1/agent/modify", json={
            "org_id": ids["org_b"], "work_id": ids["work_a"],
            "conversation_id": ids["conv_a"], "user_text": "x"})
        assert r.status_code == 404, r.text

    def test_modify_cross_work_404(self, api_client, monkeypatch):
        """service 层 EntityNotFound → 404（跨作品拒绝口径）。"""
        from backend.repositories.base import EntityNotFound
        client, ids, ctx = api_client
        self._patch_apply(
            monkeypatch, exc=EntityNotFound("Work", "跨作品修改被拒绝"))
        r = client.post("/api/v1/agent/modify", json={
            "org_id": ids["org_a"], "work_id": ids["work_a"],
            "conversation_id": ids["conv_a"], "user_text": "x"})
        assert r.status_code == 404, r.text

    def test_modify_parse_error_400(self, api_client, monkeypatch):
        from backend.services.agent.modify import ModifyParseError
        client, ids, ctx = api_client
        self._patch_apply(monkeypatch,
                          exc=ModifyParseError("无法解析修改意图"))
        r = client.post("/api/v1/agent/modify", json={
            "org_id": ids["org_a"], "work_id": ids["work_a"],
            "conversation_id": ids["conv_a"], "user_text": "x"})
        assert r.status_code == 400, r.text
        assert _err(r)["code"] == 4000, r.text
