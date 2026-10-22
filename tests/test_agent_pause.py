"""暂停/继续：pause_run / resume_run + 步骤边界协作式挂起。

- pause_run：置 paused 标记（幂等）；终态/非 queued-running-pausing 状态拒绝
- execute_run_steps：步骤边界检测到 paused → 落 run_status=paused 并返回挂起信号
  （queue 保持 task running 不落终态，与 waiting_user 同机制）
- resume_run：清标记；已挂起 → requeue=True（路由层重入队）；worker 仍在跑
  （未走到边界）→ 只清标记不重入队，避免双 worker 并发

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。

运行：./venv/bin/python -m pytest tests/test_agent_pause.py
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    reset_engine,
)
from backend.models import Membership, Organization  # noqa: E402
from backend.ai_tasks.repository import AITaskRepository  # noqa: E402
from backend.services.agent import (  # noqa: E402
    TASK_TYPE_AGENT_RUN,
    ToolResult,
    create_agent_run,
    execute_run_steps,
    get_run_state,
    pause_run,
    resume_run,
    set_run_plan,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/pause.db"
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


def _org(db, name="暂停测试合作社"):
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    return org


def _member(db, org_id, user_id="u1", role="owner"):
    db.add(Membership(org_id=org_id, user_id=user_id, role=role))
    db.commit()


def _make_run(db, org, plan=None):
    _member(db, org.id, "u1", "owner")
    task_id = create_agent_run(
        db, "u1", org.id, brief_json={"goal": "暂停测试"})
    if plan is not None:
        set_run_plan(db, task_id, org.id, plan)
    return task_id


def _plan(*steps):
    return {"steps": list(steps)}


def _ok_tool(calls):
    def fake(deps, tool_id, params, confirmed=False):
        calls.append(tool_id)
        return ToolResult(status="ok", data={})
    return fake


def _payload(db, task_id):
    return AITaskRepository(db).get(task_id).payload_json or {}


class TestPauseRun:
    def test_pause_sets_flag_idempotent(self, db):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}}))
        out = pause_run(db, task_id, org.id)
        assert out["paused"] is True
        assert _payload(db, task_id)["paused"] is True
        # 幂等：再调一次不报错
        out2 = pause_run(db, task_id, org.id)
        assert out2["paused"] is True

    def test_pause_rejects_terminal(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        repo = AITaskRepository(db)
        repo.set_status(task_id, "running")
        repo.set_status(task_id, "succeeded")
        with pytest.raises(ValueError):
            pause_run(db, task_id, org.id)

    def test_pause_rejects_waiting_user(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        repo = AITaskRepository(db)
        p = _payload(db, task_id)
        p["run_status"] = "waiting_user"
        repo.update_payload(task_id, p)
        with pytest.raises(ValueError):
            pause_run(db, task_id, org.id)


class TestExecuteSuspendsOnPause:
    def test_boundary_suspend(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}}))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        # 执行前用户点了暂停
        pause_run(db, task_id, org.id)
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "paused"
        assert calls == [], "暂停后不应再执行任何步骤"
        p = _payload(db, task_id)
        assert p["run_status"] == "paused"
        assert p["paused"] is True
        # queue 视角：task 保持 running（由上层 _execute 保证不落终态）
        assert AITaskRepository(db).get(task_id).status == "running"

    def test_pause_mid_run_full_cycle(self, db, monkeypatch):
        """跑一步 → 暂停 → 挂起 → 继续 → 跑完剩余。"""
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}}))
        repo = AITaskRepository(db)
        calls = []

        def fake(deps, tool_id, params, confirmed=False):
            calls.append(tool_id)
            if tool_id == "query_facts":
                # 第一步跑完后用户点了暂停（模拟 pause 接口写入）
                p = dict(repo.get(task_id).payload_json or {})
                p["paused"] = True
                repo.update_payload(task_id, p)
            return ToolResult(status="ok", data={})

        monkeypatch.setattr("backend.services.agent.runner.run_tool", fake)
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "paused"
        assert calls == ["query_facts"], "只跑完当前步骤即挂起"
        # 继续：已挂起 → 需要重入队
        r = resume_run(db, task_id, org.id)
        assert r["requeue"] is True
        assert r["paused"] is False
        # 模拟 worker 恢复执行：剩余步骤跑完，已成功的去重跳过
        calls2 = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls2))
        out2 = execute_run_steps(db, task_id, lambda: False)
        assert out2["run_status"] == "completed"
        assert calls2 == ["query_products"]

    def test_get_run_state_shows_paused(self, db, monkeypatch):
        org = _org(db)
        task_id = _make_run(db, org, plan=_plan(
            {"tool_id": "query_facts", "params": {}}))
        calls = []
        monkeypatch.setattr("backend.services.agent.runner.run_tool",
                            _ok_tool(calls))
        pause_run(db, task_id, org.id)
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "paused"
        st = get_run_state(db, task_id, org.id)
        assert st["run_status"] == "paused"


class TestResumeRun:
    def test_resume_requeue_when_suspended(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        repo = AITaskRepository(db)
        p = _payload(db, task_id)
        p["paused"] = True
        p["run_status"] = "paused"
        repo.update_payload(task_id, p)
        out = resume_run(db, task_id, org.id)
        assert out == {"run_status": "running", "task_id": task_id,
                       "paused": False, "requeue": True,
                       "note": "已继续；剩余步骤由 worker 异步恢复执行"}
        p2 = _payload(db, task_id)
        assert p2["paused"] is False
        assert p2["run_status"] == "running"

    def test_resume_no_requeue_when_worker_active(self, db):
        """暂停已请求但 worker 还没走到边界（run_status 仍 running）：
        只清标记，不重入队。"""
        org = _org(db)
        task_id = _make_run(db, org)
        repo = AITaskRepository(db)
        p = _payload(db, task_id)
        p["paused"] = True
        p["run_status"] = "running"
        repo.update_payload(task_id, p)
        out = resume_run(db, task_id, org.id)
        assert out["requeue"] is False
        assert out["run_status"] == "running"
        assert _payload(db, task_id)["paused"] is False

    def test_resume_rejects_not_paused(self, db):
        org = _org(db)
        task_id = _make_run(db, org)
        with pytest.raises(ValueError):
            resume_run(db, task_id, org.id)
