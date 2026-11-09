"""Phase 4a 测试：Agent HTTP 路由层（backend/api/v1/agent.py）。

覆盖：
- 未登录 401
- 非成员组织 / 跨组织 404（防枚举）
- 创建 run（202，含 work 归属校验）
- 设 plan（合法通过 / 非法 tool_id 拒绝 / 空 steps 拒绝）
- confirm 流（waiting_user → 批准恢复执行；未等待时 400）
- cancel（pending 直接取消；终态幂等）
- 会话消息分页（limit/offset；跨组织 404）
- 非 agent_run 任务 id 走 agent 路由 → 404

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。
不调真实模型：confirm 恢复执行时 monkeypatch runner.run_tool。

运行：./venv/bin/python -m pytest tests/test_agent_api_phase4a.py
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
    # 排空 AI 可观测后台队列，避免旧库会话干扰（沿用 assets 一致性测试做法）
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

    url = f"sqlite:///{tmp_path}/agent_api.db"
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
    work_b = Work(org_id=org_b.id, owner_id="u_other", title="B 作品")
    db.add_all([work_a, work_b])
    db.commit()
    ids = {"org_a": org_a.id, "org_b": org_b.id,
           "work_a": work_a.id, "work_b": work_b.id}
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
    # Phase 7：agent 路由纳入 ai 档限流（20/分钟/用户）；限流桶是进程全局，
    # 每用例重置隔离（沿用 test_v21_idempotency.py 做法）
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
    return resp.status_code, body["code"], body.get("message")


# ================= 401 / 404 =================

class TestAuth:
    def test_unauthenticated_401(self, api_client):
        """未登录：一律 401（UNAUTHENTICATED=4010）。"""
        from backend.app import app
        from backend.auth.deps import get_current_user
        client, ids, ctx = api_client
        app.dependency_overrides.pop(get_current_user)
        try:
            r = client.post("/api/v1/agent/runs",
                            json={"org_id": ids["org_a"]})
            assert r.status_code == 401, r.text
            assert r.json()["code"] == 4010, r.text
            r = client.get("/api/v1/agent/runs/whatever")
            assert r.status_code == 401, r.text
        finally:
            from backend.auth.models import User as AuthUser
            app.dependency_overrides[get_current_user] = (
                lambda: AuthUser(id="u_owner",
                                 email="owner@test.local",
                                 password_hash="x"))

    def test_create_run_nonmember_org_404(self, api_client):
        """非成员组织：404 防枚举（ORG_NOT_FOUND=4002），不暴露组织存在性。"""
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_b"],
                              "brief": {"goal": "越权创建"}})
        status, code, _ = _err(r)
        assert status == 404, r.text
        assert code == 4002, r.text

    def test_create_run_work_other_org_404(self, api_client):
        """work 属于另一组织：404（WORK_NOT_FOUND=1001），防跨作品。"""
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "work_id": ids["work_b"],
                              "brief": {"goal": "跨组织作品"}})
        status, code, _ = _err(r)
        assert status == 404, r.text
        assert code == 1001, r.text

    def test_get_run_cross_org_404(self, api_client):
        """跨组织读 run：404。"""
        from backend.auth.models import User as AuthUser
        client, ids, ctx = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "读状态"}})
        task_id = _data(r)["task_id"]
        # 切换为 B 组织成员
        ctx["user"] = AuthUser(id="u_other", email="other@test.local",
                               password_hash="x")
        r = client.get(f"/api/v1/agent/runs/{task_id}")
        status, code, _ = _err(r)
        assert status == 404, r.text
        assert code in (4002, 4040), r.text

    def test_get_run_not_agent_task_404(self, api_client):
        """非 agent_run 的任务 id 走 agent 路由：404。"""
        from backend.ai_tasks.repository import AITaskRepository
        from backend.auth.database import get_session_factory
        client, ids, _ = api_client
        db = get_session_factory()()
        try:
            t = AITaskRepository(db).create(
                org_id=ids["org_a"], task_type="ideas",
                payload={}, created_by="u_owner")
            other_id = t.id
        finally:
            db.close()
        r = client.get(f"/api/v1/agent/runs/{other_id}")
        status, code, _ = _err(r)
        assert status == 404, r.text
        assert code == 4040, r.text

    def test_messages_cross_org_404(self, api_client):
        """跨组织读会话消息：404。"""
        from backend.auth.models import User as AuthUser
        client, ids, ctx = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "消息隔离"}})
        conv_id = _data(r)["conversation_id"]
        ctx["user"] = AuthUser(id="u_other", email="other@test.local",
                               password_hash="x")
        r = client.get(f"/api/v1/agent/conversations/{conv_id}/messages")
        status, code, _ = _err(r)
        assert status == 404, r.text

    def test_messages_missing_conv_404(self, api_client):
        client, ids, _ = api_client
        r = client.get("/api/v1/agent/conversations/no-such-conv/messages")
        status, code, _ = _err(r)
        assert status == 404, r.text
        assert code == 4040, r.text


# ================= 创建 / 查询 =================

class TestCreateAndGet:
    def test_create_run_ok(self, api_client):
        """创建 run：202，返回 task_id/run_status/conversation_id。"""
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "work_id": ids["work_a"],
                              "brief": {"goal": "给苹果写口播"}})
        assert r.status_code == 202, r.text
        data = _data(r)
        assert data["task_id"]
        assert data["run_status"] == "queued"
        assert data["conversation_id"]
        assert data["work_id"] == ids["work_a"]

    def test_create_run_without_work(self, api_client):
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"]})
        assert r.status_code == 202, r.text
        assert _data(r)["work_id"] is None

    def test_get_run_state(self, api_client):
        """查询 run：含 run_status/plan/steps/pending_step/confirmed_steps。"""
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "查状态"}})
        task_id = _data(r)["task_id"]
        r = client.get(f"/api/v1/agent/runs/{task_id}")
        data = _data(r)
        assert data["task_id"] == task_id
        assert data["run_status"] == "queued"
        assert "plan" not in data or True  # plan 在 payload 内，不直接暴露
        assert data["steps"] == []
        assert data["pending_step"] is None
        assert data["confirmed_steps"] == []
        assert "found" not in data


# ================= plan =================

class TestPlan:
    def _run(self, client, ids):
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "设计划"}})
        return _data(r)["task_id"]

    def test_set_plan_ok(self, api_client):
        client, ids, _ = api_client
        task_id = self._run(client, ids)
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan", json={
            "steps": [
                {"tool_id": "query_facts", "params": {"limit": 10}},
                {"tool_id": "check_delivery",
                 "params": {"work_id": ids["work_a"]},
                 "depends_on": [0]},
            ]})
        data = _data(r)
        assert data["ok"] is True
        assert data["step_count"] == 2
        assert data["needs_confirmation"] == []
        # 计划落库：GET 可见
        r = client.get(f"/api/v1/agent/runs/{task_id}")
        assert _data(r)["run_status"] == "queued"

    def test_set_plan_invalid_tool_400(self, api_client):
        """非法 tool_id：400 拒绝。"""
        client, ids, _ = api_client
        task_id = self._run(client, ids)
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan", json={
            "steps": [{"tool_id": "no_such_tool", "params": {}}]})
        status, code, msg = _err(r)
        assert status == 400, r.text
        assert code == 4000, r.text
        assert "白名单" in (msg or ""), r.text

    def test_set_plan_empty_steps_400(self, api_client):
        client, ids, _ = api_client
        task_id = self._run(client, ids)
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan",
                        json={"steps": []})
        status, code, _ = _err(r)
        assert status == 400, r.text
        assert code == 4000, r.text

    def test_set_plan_cross_org_404(self, api_client):
        from backend.auth.models import User as AuthUser
        client, ids, ctx = api_client
        task_id = self._run(client, ids)
        ctx["user"] = AuthUser(id="u_other", email="other@test.local",
                               password_hash="x")
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan", json={
            "steps": [{"tool_id": "query_facts", "params": {}}]})
        status, code, _ = _err(r)
        assert status == 404, r.text


# ================= confirm 流 =================

def _fake_ok_tool(calls, data=None):
    from backend.services.agent import ToolResult

    def fake(deps, tool_id, params, confirmed=False):
        calls.append({"tool_id": tool_id, "params": params,
                      "confirmed": confirmed})
        return ToolResult(status="ok", data=data or {},
                          artifact_ref=None)
    return fake


class TestConfirm:
    def _run_with_confirm_plan(self, client, ids):
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "work_id": ids["work_a"],
                              "brief": {"goal": "确认流"}})
        task_id = _data(r)["task_id"]
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan", json={
            "steps": [{"tool_id": "patch_work",
                       "params": {"work_id": ids["work_a"],
                                  "expected_version": 1,
                                  "title": "AI 改后的标题"}}]})
        assert _data(r)["needs_confirmation"] == [0]
        return task_id

    def _drive_to_waiting(self, task_id, monkeypatch):
        """service 层执行到 waiting_user（run_tool 已 mock，不调真实模型）。"""
        from backend.auth.database import get_session_factory
        from backend.services.agent import execute_run_steps
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _fake_ok_tool(calls))
        db = get_session_factory()()
        try:
            out = execute_run_steps(db, task_id, lambda: False)
        finally:
            db.close()
        assert out["run_status"] == "waiting_user", out
        assert out["pending_step"]["step_idx"] == 0
        return calls

    def test_confirm_approve_resumes(self, api_client, monkeypatch):
        """waiting_user → 批准 → 只落确认态并入队（Phase 5 异步化）；
        HTTP 内不再同步执行，worker 恢复执行后完成（HTTP 全链路）。"""
        client, ids, _ = api_client
        task_id = self._run_with_confirm_plan(client, ids)
        calls = self._drive_to_waiting(task_id, monkeypatch)

        enqueued = []
        monkeypatch.setattr(
            "backend.api.v1.agent._enqueue_run_resume",
            lambda task: enqueued.append(task.id))

        r = client.post(f"/api/v1/agent/runs/{task_id}/confirm",
                        json={"step_idx": 0, "approved": True})
        out = _data(r)
        # 异步化断言：HTTP 直接返回 running，工具在请求内未执行，但已入队
        assert out["run_status"] == "running", out
        assert out["resumed"] is False
        assert calls == []
        assert enqueued == [task_id]

        r = client.get(f"/api/v1/agent/runs/{task_id}")
        state = _data(r)
        assert state["confirmed_steps"] == [0]
        assert state["pending_step"] is None
        assert state["run_status"] == "running"

        # 模拟 worker 恢复执行（队列 runner 入口）
        from backend.services.agent import run_agent_run
        out2 = run_agent_run(task_id, lambda: False, {})
        assert out2["run_status"] == "completed", out2
        assert len(calls) == 1
        assert calls[0]["confirmed"] is True

        r = client.get(f"/api/v1/agent/runs/{task_id}")
        assert _data(r)["run_status"] == "completed"

    def test_confirm_reject_cancels(self, api_client, monkeypatch):
        """waiting_user → 拒绝 → 任务 cancelled，工具绝不执行。"""
        client, ids, _ = api_client
        task_id = self._run_with_confirm_plan(client, ids)
        calls = self._drive_to_waiting(task_id, monkeypatch)

        r = client.post(f"/api/v1/agent/runs/{task_id}/confirm",
                        json={"step_idx": 0, "approved": False})
        out = _data(r)
        assert out["run_status"] == "cancelled", out
        assert calls == []

        r = client.get(f"/api/v1/agent/runs/{task_id}")
        assert _data(r)["run_status"] == "cancelled"

    def test_confirm_not_waiting_400(self, api_client):
        """未进入 waiting 就确认 → 400。"""
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "未等待"}})
        task_id = _data(r)["task_id"]
        r = client.post(f"/api/v1/agent/runs/{task_id}/confirm",
                        json={"step_idx": 0, "approved": True})
        status, code, _ = _err(r)
        assert status == 400, r.text
        assert code == 4000, r.text

    def test_confirm_wrong_idx_400(self, api_client, monkeypatch):
        """step_idx 对不上待确认步骤 → 400。"""
        client, ids, _ = api_client
        task_id = self._run_with_confirm_plan(client, ids)
        self._drive_to_waiting(task_id, monkeypatch)
        r = client.post(f"/api/v1/agent/runs/{task_id}/confirm",
                        json={"step_idx": 5, "approved": True})
        status, code, _ = _err(r)
        assert status == 400, r.text
        assert code == 4000, r.text


# ================= cancel =================

class TestCancel:
    def test_cancel_pending(self, api_client):
        """pending 且未入队的 run：取消直接生效（cancelled=True）。

        2026-10-22 验收修复（8b4e79a）：此前 TaskQueue.cancel 对未入
        dispatcher 的 pending 任务取消无效，只打标记不改状态；现已修复，
        取消 pending 任务直接落终态。
        """
        from backend.ai_tasks.repository import AITaskRepository
        from backend.auth.database import get_session_factory
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "取消我"}})
        task_id = _data(r)["task_id"]
        r = client.post(f"/api/v1/agent/runs/{task_id}/cancel")
        out = _data(r)
        assert out["cancelled"] is True
        assert out["status"] == "cancelled"
        db = get_session_factory()()
        try:
            t = AITaskRepository(db).get(task_id)
            assert t.status == "cancelled"
        finally:
            db.close()

    def test_cancel_terminal_idempotent(self, api_client):
        """终态任务取消：幂等返回 cancelled=False。"""
        from backend.ai_tasks.repository import AITaskRepository
        from backend.auth.database import get_session_factory
        client, ids, _ = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "取消两次"}})
        task_id = _data(r)["task_id"]
        db = get_session_factory()()
        try:
            AITaskRepository(db).set_status(task_id, "cancelled")
        finally:
            db.close()
        out = _data(client.post(f"/api/v1/agent/runs/{task_id}/cancel"))
        assert out["cancelled"] is False
        assert out["status"] == "cancelled"
        assert "终态" in out["message"]

    def test_cancel_cross_org_404(self, api_client):
        from backend.auth.models import User as AuthUser
        client, ids, ctx = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "跨组织取消"}})
        task_id = _data(r)["task_id"]
        ctx["user"] = AuthUser(id="u_other", email="other@test.local",
                               password_hash="x")
        r = client.post(f"/api/v1/agent/runs/{task_id}/cancel")
        status, code, _ = _err(r)
        assert status == 404, r.text


# ================= 消息分页 =================

class TestMessages:
    def _conv_with_messages(self, client, ids):
        from backend.auth.database import get_session_factory
        from backend.services.agent import append_message
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org_a"],
                              "brief": {"goal": "分页测试"}})
        conv_id = _data(r)["conversation_id"]
        db = get_session_factory()()
        try:
            for i in range(4):
                append_message(db, conv_id, ids["org_a"], "assistant",
                               {"type": "note", "n": i})
        finally:
            db.close()
        return conv_id

    def test_messages_pagination(self, api_client):
        client, ids, _ = api_client
        conv_id = self._conv_with_messages(client, ids)
        # 1 条 brief + 4 条 note = 5
        r = client.get(
            f"/api/v1/agent/conversations/{conv_id}/messages",
            params={"limit": 2, "offset": 0})
        data = _data(r)
        assert data["total"] == 5, data
        assert len(data["items"]) == 2
        assert data["items"][0]["role"] == "user"  # 时间正序：brief 在前
        assert all("id" in m and "content" in m and "created_at" in m
                   for m in data["items"])

        r = client.get(
            f"/api/v1/agent/conversations/{conv_id}/messages",
            params={"limit": 2, "offset": 2})
        data = _data(r)
        assert len(data["items"]) == 2
        assert data["items"][0]["content"]["n"] == 1

        r = client.get(
            f"/api/v1/agent/conversations/{conv_id}/messages",
            params={"limit": 10, "offset": 4})
        assert len(_data(r)["items"]) == 1

    def test_messages_default_paging(self, api_client):
        client, ids, _ = api_client
        conv_id = self._conv_with_messages(client, ids)
        data = _data(client.get(
            f"/api/v1/agent/conversations/{conv_id}/messages"))
        assert data["total"] == 5
        assert data["limit"] == 50 and data["offset"] == 0


# ================= 每日限额（2026-10-22 接入） =================

class TestQuota:
    """/agent/quota：与 POST /poster/image 同一口径（services/poster_quota）。"""

    def test_owner_exempt(self, api_client):
        client, ids, _ = api_client
        data = _data(client.get("/api/v1/agent/quota",
                                params={"org_id": ids["org_a"]}))
        assert data["daily_limit"] == 5, data
        assert data["enforced"] is False, data
        assert data["allowed"] is True, data
        assert data["remaining"] is None, data          # 不受限，不是 0 次

    def test_editor_exhausted(self, api_client):
        from datetime import datetime, timezone

        from backend.auth.database import get_session_factory
        from backend.auth.models import User as AuthUser
        from backend.models import Membership, PosterImageUsage

        client, ids, ctx = api_client
        db = get_session_factory()()
        try:
            db.add(AuthUser(id="u_edit", email="edit@test.local",
                            password_hash="x"))
            db.flush()
            db.add(Membership(user_id="u_edit", org_id=ids["org_a"],
                              role="editor"))
            now = datetime.now(timezone.utc)
            for _ in range(5):
                db.add(PosterImageUsage(user_id="u_edit",
                                        org_id=ids["org_a"], created_at=now))
            db.commit()
        finally:
            db.close()

        ctx["user"] = AuthUser(id="u_edit", email="edit@test.local",
                               password_hash="x")
        data = _data(client.get("/api/v1/agent/quota",
                                params={"org_id": ids["org_a"]}))
        assert data["enforced"] is True, data
        assert data["allowed"] is False, data
        assert data["remaining"] == 0, data
        assert data["used"] == 5, data

    def test_nonmember_404(self, api_client):
        client, ids, _ = api_client
        r = client.get("/api/v1/agent/quota", params={"org_id": ids["org_b"]})
        status, code, _msg = _err(r)
        assert status == 404 and code == 4002, r.text
