"""Phase 3 测试：对话持久化 + agent_run 生命周期。

- conversations/conversation_messages CRUD、分页、role 校验、org 隔离
- create_agent_run：建 AITask(task_type="agent_run") + conversation
- 步骤执行（mock run_tool，不调真实模型）：成功 / 去重 / partial /
  必需失败 / 取消 / waiting_user 确认流 / 拒绝 / 刷新恢复
- RUNNERS 注册 agent_run；queue 挂起时保持 running（不误标 succeeded）
- 迁移 0020：空库 upgrade/downgrade/upgrade；scratch 库验证

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。

运行：./venv/bin/python -m pytest tests/test_agent_run_phase3.py
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    reset_engine,
)
from backend.models import Membership, Organization, Work, TASK_TYPES  # noqa: E402
from backend.repositories.base import EntityNotFound  # noqa: E402
from backend.ai_tasks.repository import AITaskRepository  # noqa: E402
from backend.ai_tasks.runners import RUNNERS  # noqa: E402
from backend.ai_tasks.queue import TaskQueue, _TaskCancelled  # noqa: E402
from backend.services.agent import (  # noqa: E402
    AgentIdentity,
    AgentStep,
    TASK_TYPE_AGENT_RUN,
    ToolResult,
    append_message,
    confirm_step,
    count_messages,
    create_agent_run,
    create_conversation,
    execute_run_steps,
    get_conversation,
    get_conversation_or_404,
    get_run_state,
    list_conversations,
    list_messages,
    run_agent_run,
    set_conversation_status,
    set_run_plan,
    step_params_digest,
)


# ---------------------------------------------------------------------------
# fixture：每测试一个 scratch 库（alembic 升到 head）
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/phase3.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()
    reset_engine()


def _org(db, name="测试合作社"):
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _member(db, org_id, user_id="u1", role="owner"):
    db.add(Membership(user_id=user_id, org_id=org_id, role=role))
    db.commit()


def _work(db, org_id, title="测试作品"):
    w = Work(org_id=org_id, owner_id="u1", title=title)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _ident(user_id="u1", org_id=""):
    return AgentIdentity(user_id=user_id, org_id=org_id, role="owner")


def _plan(*steps):
    return {"steps": list(steps)}


def _ok_tool(calls, data=None, artifact_ref=None):
    def fake(deps, tool_id, params, confirmed=False):
        calls.append({"tool_id": tool_id, "params": params,
                      "confirmed": confirmed})
        return ToolResult(status="ok", data=data or {},
                          artifact_ref=artifact_ref)
    return fake


def _failing_tool(calls, fail_on):
    def fake(deps, tool_id, params, confirmed=False):
        calls.append({"tool_id": tool_id, "params": params,
                      "confirmed": confirmed})
        if tool_id in fail_on:
            return ToolResult(status="failed", error_code="internal",
                              error_message="模拟失败")
        return ToolResult(status="ok", data={})
    return fake


def _make_run(db, org, work=None, brief=None, plan=None):
    _member(db, org.id, "u1", "owner")
    task_id = create_agent_run(
        db, _ident("u1", org.id), org.id,
        work_id=work.id if work else None,
        brief_json=brief or {"goal": "测试目标"})
    if plan is not None:
        set_run_plan(db, task_id, org.id, plan)
    return task_id


# ================= 对话持久化 =================

class TestConversationPersistence:
    def test_create_get_list(self, db):
        org = _org(db)
        conv = create_conversation(db, org.id, "u1", brief={"goal": "g"})
        assert conv.id and conv.org_id == org.id
        assert get_conversation(db, conv.id, org.id).id == conv.id
        assert get_conversation_or_404(db, conv.id, org.id).id == conv.id
        lst = list_conversations(db, org.id)
        assert [c.id for c in lst] == [conv.id]
        # 状态变更
        set_conversation_status(db, conv.id, org.id, "archived")
        assert get_conversation(db, conv.id, org.id).status == "archived"

    def test_append_and_paginate(self, db):
        org = _org(db)
        conv = create_conversation(db, org.id, "u1")
        for i in range(5):
            append_message(db, conv.id, org.id, "user",
                           {"text": f"hi-{i}"})
        assert count_messages(db, conv.id, org.id) == 5
        page1 = list_messages(db, conv.id, org.id, limit=2, offset=0)
        page2 = list_messages(db, conv.id, org.id, limit=2, offset=2)
        assert [m.content_json["text"] for m in page1] == ["hi-0", "hi-1"]
        assert [m.content_json["text"] for m in page2] == ["hi-2", "hi-3"]

    def test_role_validation(self, db):
        org = _org(db)
        conv = create_conversation(db, org.id, "u1")
        with pytest.raises(ValueError):
            append_message(db, conv.id, org.id, "system", {"text": "x"})
        for role in ("user", "assistant", "tool"):
            append_message(db, conv.id, org.id, role, {"text": role})
        assert count_messages(db, conv.id, org.id) == 3

    def test_cross_org_rejected(self, db):
        """跨组织读/写一律 EntityNotFound（404 防枚举）。"""
        org_a, org_b = _org(db, "A"), _org(db, "B")
        conv = create_conversation(db, org_a.id, "u1")
        assert get_conversation(db, conv.id, org_b.id) is None
        with pytest.raises(EntityNotFound):
            get_conversation_or_404(db, conv.id, org_b.id)
        with pytest.raises(EntityNotFound):
            append_message(db, conv.id, org_b.id, "user", {"text": "x"})
        with pytest.raises(EntityNotFound):
            list_messages(db, conv.id, org_b.id)
        assert list_conversations(db, org_b.id) == []

    def test_list_filters(self, db):
        org = _org(db)
        w = _work(db, org.id)
        c1 = create_conversation(db, org.id, "u1", work_id=w.id)
        create_conversation(db, org.id, "u2")
        assert {c.id for c in list_conversations(db, org.id,
                                                user_id="u1")} == {c1.id}
        assert {c.id for c in list_conversations(db, org.id,
                                                work_id=w.id)} == {c1.id}


# ================= run 创建 =================

class TestCreateAgentRun:
    def test_create_ok(self, db):
        org = _org(db)
        _member(db, org.id, "u1", "owner")
        work = _work(db, org.id)
        task_id = create_agent_run(db, _ident("u1", org.id), org.id,
                                   work_id=work.id,
                                   brief_json={"goal": "做条苹果视频"})
        repo = AITaskRepository(db)
        task = repo.get(task_id, org.id)
        assert task.task_type == TASK_TYPE_AGENT_RUN == "agent_run"
        assert task.status == "pending"
        p = task.payload_json
        assert p["run_status"] == "queued"
        assert p["plan"] is None
        assert p["work_id"] == work.id
        assert p["brief"] == {"goal": "做条苹果视频"}
        # conversation 已建且关联
        conv = get_conversation_or_404(db, p["conversation_id"], org.id)
        assert conv.work_id == work.id
        # brief 已作为 user 消息落盘
        msgs = list_messages(db, conv.id, org.id)
        assert msgs[0].role == "user"
        assert msgs[0].content_json["type"] == "brief"

    def test_rejects_cross_org_work(self, db):
        """跨作品 ID 拒绝：A 组织的作品不能绑到 B 组织的 run。"""
        org_a, org_b = _org(db, "A"), _org(db, "B")
        _member(db, org_b.id, "u1", "owner")
        work_a = _work(db, org_a.id)
        with pytest.raises(EntityNotFound):
            create_agent_run(db, _ident("u1", org_b.id), org_b.id,
                             work_id=work_a.id, brief_json={})

    def test_rejects_non_member(self, db):
        org = _org(db)
        with pytest.raises(EntityNotFound):
            create_agent_run(db, _ident("stranger", org.id), org.id,
                             brief_json={})

    def test_set_run_plan_ok_and_invalid(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        v = set_run_plan(db, task_id, org.id, _plan(
            {"tool_id": "query_facts", "params": {}}))
        assert v.ok
        task = AITaskRepository(db).get(task_id, org.id)
        assert task.payload_json["plan"]["steps"][0]["tool_id"] \
            == "query_facts"
        assert task.payload_json["run_status"] == "queued"
        with pytest.raises(ValueError):
            set_run_plan(db, task_id, org.id,
                         {"steps": [{"tool_id": "不存在", "params": {}}]})
        # 跨组织写 plan 拒绝
        org_b = _org(db, "B")
        with pytest.raises(EntityNotFound):
            set_run_plan(db, task_id, org_b.id,
                         _plan({"tool_id": "query_facts", "params": {}}))


# ================= 步骤执行 =================

class TestRunExecution:
    def test_steps_success(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {"keyword": "苹果"}},
        ))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls, artifact_ref="art-1"))
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "completed"
        task = AITaskRepository(db).get(task_id)
        assert task.status == "succeeded"
        assert task.payload_json["run_status"] == "completed"
        assert task.progress == 100
        steps = db.query(AgentStep).filter_by(task_id=task_id).all()
        assert len(steps) == 2
        assert all(s.status == "succeeded" for s in steps)
        assert all(s.duration_ms is not None and s.duration_ms >= 0
                   for s in steps)
        assert steps[0].output_ref == "art-1"
        # 去重键一致
        assert steps[0].params_digest == step_params_digest({})
        # tool 消息落盘
        conv_id = task.payload_json["conversation_id"]
        roles = [m.role for m in list_messages(db, conv_id, org.id)]
        assert "tool" in roles and roles[-1] == "assistant"

    def test_step_dedup(self, db, monkeypatch):
        """预置 succeeded 行 → 该步不再执行；全命中 → 零调用完成。"""
        org = _org(db)
        plan = _plan({"tool_id": "query_facts", "params": {}},
                     {"tool_id": "query_products", "params": {}})
        task_id = _make_run(db, org, plan=plan)
        for s in plan["steps"]:
            db.add(AgentStep(task_id=task_id, tool_id=s["tool_id"],
                             params_digest=step_params_digest(s["params"]),
                             status="succeeded",
                             started_at=None, finished_at=None))
        db.commit()
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "completed"
        assert calls == []
        assert out["summary"]["dedup_skipped"] == 2

    def test_partial_on_optional_failure(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {},
             "optional": True},
        ))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _failing_tool(calls, {"query_products"}))
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "partial"
        task = AITaskRepository(db).get(task_id)
        # succeeded + partial：任务 succeeded，run_status 标记 partial
        assert task.status == "succeeded"
        assert task.payload_json["run_status"] == "partial"
        statuses = {s.tool_id: s.status for s in
                    db.query(AgentStep).filter_by(task_id=task_id).all()}
        assert statuses == {"query_facts": "succeeded",
                            "query_products": "failed"}

    def test_required_failure(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}},
        ))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _failing_tool(calls, {"query_products"}))
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "failed"
        assert out["failed_step"] == 1
        task = AITaskRepository(db).get(task_id)
        assert task.status == "failed"
        assert task.payload_json["run_status"] == "failed"
        assert "模拟失败" in (task.error or "")

    def test_cancel_never_reports_completed(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}},
            {"tool_id": "query_facts", "params": {"limit": 10}},
        ))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))

        def is_cancelled():
            return len(calls) >= 1  # 第一步后取消

        with pytest.raises(_TaskCancelled):
            execute_run_steps(db, task_id, is_cancelled)
        task = AITaskRepository(db).get(task_id)
        assert task.payload_json["run_status"] == "cancelled"
        # 模拟 queue._execute 的取消落库：绝不误报 completed/succeeded
        AITaskRepository(db).set_status(task_id, "cancelled",
                                        error="任务被取消")
        task = AITaskRepository(db).get(task_id)
        assert task.status == "cancelled"
        assert task.payload_json["run_status"] == "cancelled"
        assert task.status != "succeeded"

    def test_no_plan_fails_clearly(self, db):
        org = _org(db)
        task_id = _make_run(db, org)  # plan=None
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "failed"
        assert "缺少执行计划" in out["error"]
        assert AITaskRepository(db).get(task_id).status == "failed"


# ================= waiting_user 确认流 =================

def _confirm_plan(work_id):
    return _plan(
        {"tool_id": "patch_work",
         "params": {"work_id": work_id, "expected_version": 1,
                    "title": "AI 改后的标题"}},
    )


class TestWaitingUser:
    def test_wait_confirm_resume(self, db, monkeypatch):
        """waiting_user → 确认只落确认态（Phase 5 改异步）；worker 恢复执行后完成。"""
        org = _org(db)
        work = _work(db, org.id)
        task_id = _make_run(db, org, work=work,
                            plan=_confirm_plan(work.id))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "waiting_user"
        assert out["pending_step"]["step_idx"] == 0
        assert out["pending_step"]["tool_id"] == "patch_work"
        # 挂起时任务保持 running，不落终态
        task = AITaskRepository(db).get(task_id)
        assert task.status == "running"
        assert task.payload_json["run_status"] == "waiting_user"
        # 未确认前工具未执行
        assert calls == []
        # 用户确认 → 只落确认态（Phase 5 异步化：HTTP 内不再同步执行）
        out2 = confirm_step(db, task_id, 0, True)
        assert out2["run_status"] == "running"
        assert out2["resumed"] is False
        assert calls == []  # 确认时工具仍未执行
        task = AITaskRepository(db).get(task_id)
        assert task.status == "running"  # 仍是 running，未被误标终态
        assert task.payload_json["confirmed_steps"] == [0]
        assert task.payload_json["pending_step"] is None
        # worker 恢复执行（模拟队列 worker 调 run_agent_run）
        out3 = run_agent_run(task_id, lambda: False, {"_org_id": org.id})
        assert out3["run_status"] == "completed"
        assert len(calls) == 1
        assert calls[0]["confirmed"] is True
        db.expire_all()  # worker 用独立会话落库，本会话需失效重读
        task = AITaskRepository(db).get(task_id)
        assert task.status == "succeeded"
        assert task.payload_json["confirmed_steps"] == [0]

    def test_reject_cancels(self, db, monkeypatch):
        org = _org(db)
        work = _work(db, org.id)
        task_id = _make_run(db, org, work=work,
                            plan=_confirm_plan(work.id))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        execute_run_steps(db, task_id, lambda: False)
        out = confirm_step(db, task_id, 0, False)
        assert out["run_status"] == "cancelled"
        task = AITaskRepository(db).get(task_id)
        assert task.status == "cancelled"
        assert calls == []  # 拒绝后工具绝不执行

    def test_confirm_wrong_state_or_idx(self, db, monkeypatch):
        org = _org(db)
        work = _work(db, org.id)
        task_id = _make_run(db, org, work=work,
                            plan=_confirm_plan(work.id))
        # 还没 waiting 就确认 → ValueError
        with pytest.raises(ValueError):
            confirm_step(db, task_id, 0, True)
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        execute_run_steps(db, task_id, lambda: False)
        # step_idx 对不上 → ValueError
        with pytest.raises(ValueError):
            confirm_step(db, task_id, 5, True)

    def test_refresh_recovery(self, db, monkeypatch):
        """刷新恢复：关闭会话后用新会话重读，waiting 状态无丢失，可确认。"""
        org = _org(db)
        work = _work(db, org.id)
        task_id = _make_run(db, org, work=work,
                            plan=_confirm_plan(work.id))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        assert execute_run_steps(db, task_id, lambda: False)[
            "run_status"] == "waiting_user"
        org_id, conv_id = org.id, AITaskRepository(db).get(
            task_id).payload_json["conversation_id"]
        db.close()  # 模拟页面刷新 / 进程重启，会话丢弃
        # 新会话重读即恢复
        db2 = get_session_factory()()
        try:
            state = get_run_state(db2, task_id, org_id)
            assert state["found"] is True
            assert state["status"] == "running"
            assert state["run_status"] == "waiting_user"
            assert state["pending_step"]["step_idx"] == 0
            assert state["pending_step"]["tool_id"] == "patch_work"
            assert state["conversation_id"] == conv_id
            assert any(s["status"] == "waiting_user"
                       for s in state["steps"])
            # 确认后只落确认态（Phase 5 异步化），worker 恢复执行才完成
            out = confirm_step(db2, task_id, 0, True)
            assert out["run_status"] == "running"
            assert out["resumed"] is False
            out2 = run_agent_run(task_id, lambda: False, {"_org_id": org.id})
            assert out2["run_status"] == "completed"
        finally:
            db2.close()

    def test_get_run_state_org_isolation(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        org_b = _org(db, "B")
        assert get_run_state(db, task_id, org_b.id)["found"] is False
        with pytest.raises(EntityNotFound):
            confirm_step(db, task_id, 0, True, org_id=org_b.id)


# ================= 注册 / queue 集成 =================

class TestRegistrationAndQueue:
    def test_runner_registered(self):
        assert "agent_run" in RUNNERS
        assert "agent_run" in TASK_TYPES

    def test_queue_keeps_running_on_waiting_user(self, db, monkeypatch):
        """真队列：waiting_user 时 _execute 不把任务标 succeeded。"""
        org = _org(db)
        work = _work(db, org.id)
        task_id = _make_run(db, org, work=work,
                            plan=_confirm_plan(work.id))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        payload = {"_org_id": org.id}

        def _runner(tid, is_cancelled):
            return RUNNERS["agent_run"](tid, is_cancelled, dict(payload))

        TaskQueue(get_session_factory()).submit(task_id, _runner)
        # 轮询等挂起（最多 15 秒）
        deadline = time.time() + 15
        seen = None
        while time.time() < deadline:
            s = get_session_factory()()
            try:
                t = AITaskRepository(s).get(task_id)
                rs = (t.payload_json or {}).get("run_status")
                seen = (t.status, rs)
                if rs == "waiting_user":
                    break
            finally:
                s.close()
            time.sleep(0.2)
        assert seen is not None and seen[1] == "waiting_user", \
            f"任务未进入 waiting_user：{seen}"
        # 关键断言：仍是 running，没有被 queue 误标 succeeded
        assert seen[0] == "running", f"queue 误标了终态：{seen}"
        # 确认只落确认态（Phase 5 异步化）；重新入队后 worker 恢复执行完成
        out = confirm_step(db, task_id, 0, True)
        assert out["run_status"] == "running"
        assert out["resumed"] is False

        def _resume(tid, is_cancelled):
            return RUNNERS["agent_run"](tid, is_cancelled, dict(payload))

        TaskQueue(get_session_factory()).submit(task_id, _resume)
        deadline = time.time() + 15
        final = None
        while time.time() < deadline:
            s = get_session_factory()()
            try:
                t = AITaskRepository(s).get(task_id)
                rs = (t.payload_json or {}).get("run_status")
                if t.status == "succeeded" and rs == "completed":
                    final = (t.status, rs)
                    break
            finally:
                s.close()
            time.sleep(0.2)
        assert final == ("succeeded", "completed"), \
            f"worker 恢复执行未完成：{final}"

    def test_run_agent_run_entry(self, db, monkeypatch):
        """run_agent_run 入口：自建会话执行（queue runner 签名）。"""
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}}))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        out = run_agent_run(task_id, lambda: False, {"_org_id": org.id})
        assert out["run_status"] == "completed"
        assert len(calls) == 1


# ================= 迁移 0020 =================

# 注意：migrations/env.py 会用 DATABASE_URL 覆盖 sqlalchemy.url，
# 因此迁移测试必须走环境变量，不能用 set_main_option。
def _alembic_cfg():
    from alembic.config import Config
    return Config(os.path.join(PROJECT_ROOT, "alembic.ini"))


def _tables(url):
    import sqlite3
    path = url.replace("sqlite:///", "")
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


class TestMigration0020:
    def test_upgrade_downgrade_upgrade_empty(self, tmp_path, monkeypatch):
        """空库：head → 0019 → head 全周期。"""
        from alembic import command
        url = f"sqlite:///{tmp_path}/empty.db"
        monkeypatch.setenv("DATABASE_URL", url)
        cfg = _alembic_cfg()
        command.upgrade(cfg, "head")
        tables = _tables(url)
        assert {"conversations", "conversation_messages",
                "agent_steps"} <= tables
        command.downgrade(cfg, "0019_ai_call_org")
        tables = _tables(url)
        assert "conversations" not in tables
        assert "conversation_messages" not in tables
        assert "agent_steps" not in tables
        command.upgrade(cfg, "head")
        tables = _tables(url)
        assert {"conversations", "conversation_messages",
                "agent_steps"} <= tables

    def test_scratch_copy_upgrade(self, tmp_path, monkeypatch):
        """scratch 拷贝：0019 → 拷贝 → head，验证增量可执行。"""
        import shutil
        from alembic import command
        src = tmp_path / "src.db"
        url = f"sqlite:///{src}"
        monkeypatch.setenv("DATABASE_URL", url)
        command.upgrade(_alembic_cfg(), "0019_ai_call_org")
        assert "conversations" not in _tables(url)
        dst = tmp_path / "scratch.db"
        shutil.copy(src, dst)
        durl = f"sqlite:///{dst}"
        monkeypatch.setenv("DATABASE_URL", durl)
        command.upgrade(_alembic_cfg(), "head")
        assert {"conversations", "conversation_messages",
                "agent_steps"} <= _tables(durl)
