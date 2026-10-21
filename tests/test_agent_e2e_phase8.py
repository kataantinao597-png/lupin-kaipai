"""Phase 8 E2E + 故障演练（任务书 §十二 Phase 8）。

E2E（TestAgentE2E::test_full_loop）：
  一句话 brief → run_one_shot_brief 建 run/作品 → 新 session 重读验证真实
  持久化 → create_agent_run + set_run_plan → 真实 execute_run_steps
 （真实步骤循环 + 真实工具 adapters，模型经 fake 注入）→ AgentStep 行与
  会话消息断言 → apply_modification 对话局部修改 → restore_modification
  版本恢复 → export-json 导出。

确定性：gateway/模型一律 fake 注入，不烧真实 token；不用固定延迟模拟，
全部用真实状态断言（本文件无后台线程，无轮询等待）。

故障演练（TestFaultDrills）：模型超时 / 429 穿透硬停 / 5xx 不无差别重试 /
工具失败（可选→partial，必需→failed）/ 任务重复提交（幂等键）/ 用户取消 /
并发编辑冲突（乐观锁）。429 硬停主路径另见
test_agent_orchestrator_phase5.py::TestRateLimit（本文件复核通过）。

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。

运行：./venv/bin/python -m pytest tests/test_agent_e2e_phase8.py
"""
import json
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
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/phase8_e2e.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import get_session_factory, reset_engine
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()
    reset_engine()


def _org(db, name="E2E 合作社"):
    from backend.models import Organization, Membership
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.flush()
    db.add(Membership(user_id="u1", org_id=org.id, role="owner"))
    db.commit()
    db.refresh(org)
    return org


def _seed_fact_product(db, org_id):
    """种子事实 + 产品：供 query_facts / query_products 真实 adapter 读取。"""
    from backend.models import Fact, Product
    p = Product(org_id=org_id, code="YT-APPLE", name="烟台苹果")
    db.add(p)
    db.flush()
    f = Fact(org_id=org_id, product_id=p.id,
             content="烟台苹果，果园现摘现发", category="产地",
             status="confirmed", allow_use=True,
             source_type="human_statement")
    db.add(f)
    db.commit()
    return p, f


# ---------------------------------------------------------------------------
# fake 模型大脑（一句话 brief 用；复用 phase5 的 prompt 标记约定）
# ---------------------------------------------------------------------------

def _base_brief(**kw):
    d = {
        "product_name": "烟台苹果", "platform": "抖音",
        "marketing_goal": "带货卖苹果",
        "target_audience": "注重健康的年轻人",
        "budget_yuan": 2000, "people_count": 2,
        "devices": ["手机", "三脚架"], "location": "果园",
        "video_duration_sec": 60, "style": "拉呱风",
    }
    d.update(kw)
    return d


def _strategy():
    return {"strategy": {
        "goals": ["7 天卖出 500 单"],
        "target_audience": "注重健康的年轻人",
        "content_pillars": ["产地直发", "现摘现发"],
        "key_messages": ["新鲜脆甜", "果园直发"]}}


def _ideas():
    return {"ideas": [
        {"title": "果园现摘", "angle": "跟着果农进果园现摘",
         "selling_points": ["现摘现发", "脆甜多汁"]},
        {"title": "开箱测评", "angle": "真实开箱试吃",
         "selling_points": ["个头匀称", "果香浓郁"]}]}


def _storyboard(duration=60, shots=5):
    out = []
    per = duration / shots
    for i in range(1, shots + 1):
        out.append({
            "shot_no": i, "scene": "果园场景%i" % i, "shot_type": "中景",
            "duration_sec": per,
            "voiceover": "第%i镜：果园里现摘的苹果，又脆又甜" % i,
            "subtitle": "字幕%i" % i, "shoot_notes": "拍摄要点%i" % i})
    return {"shots": out}


def _storyboard_from_prompt(prompt):
    import re
    m_dur = re.search(r"目标时长约 (\d+) 秒", prompt)
    m_n = re.search(r"共 (\d+) 个镜头", prompt)
    duration = int(m_dur.group(1)) if m_dur else 60
    shots = int(m_n.group(1)) if m_n else 5
    return _storyboard(duration=duration, shots=shots)


def _copy():
    return {"copy": {
        "platform": "抖音", "title": "烟台苹果，现摘现发",
        "text": "果园现摘的苹果，又脆又甜，欢迎下单",
        "hashtags": ["烟台苹果", "助农"]}}


def _one_shot_brain():
    calls = {"n": 0}

    def _kind(prompt):
        if "brief 解析器" in prompt:
            return "brief"
        if "营销策略师" in prompt:
            return "strategy"
        if "营销策划师" in prompt:
            return "ideas"
        if "分镜师" in prompt:
            return "storyboard"
        if "平台文案写手" in prompt:
            return "copy"
        return "other"

    def fake(prompt, feedback=None):
        calls["n"] += 1
        kind = _kind(prompt)
        if kind == "brief":
            return _base_brief()
        if kind == "strategy":
            return _strategy()
        if kind == "ideas":
            return _ideas()
        if kind == "storyboard":
            return _storyboard_from_prompt(prompt)
        if kind == "copy":
            return _copy()
        raise AssertionError("未预期的模型调用: %s" % prompt[:60])

    fake.calls = calls
    return fake


def _ideas_brain():
    """plan 执行用：只服务 generate_ideas 的“营销策划师”prompt。"""
    def fake(prompt, feedback=None):
        assert "营销策划师" in prompt, prompt[:60]
        return _ideas()
    return fake


class _ModifyBrain:
    """局部修改用 fake：意图解析 + 内容生成。"""

    def __init__(self, plan, updates):
        self.plan = plan
        self.updates = updates
        self.calls = []

    def __call__(self, prompt, feedback=None):
        import copy as _copy_mod
        self.calls.append(prompt)
        if "【修改意图解析】" in prompt:
            return _copy_mod.deepcopy(self.plan)
        if "【局部重写" in prompt:
            return {"updates": _copy_mod.deepcopy(self.updates)}
        raise AssertionError("未预期的模型调用: %s" % prompt[:60])


# ================= E2E：完整闭环 =================

class TestAgentE2E:
    def test_full_loop(self, db, monkeypatch):
        from backend.services.agent import (
            run_one_shot_brief, create_agent_run, set_run_plan,
            execute_run_steps, apply_modification, restore_modification)
        from backend.services.agent.runner import AgentStep
        from backend.services.agent.conversations import list_messages
        from backend.services.agent.quality_gates import verify_persistence
        from backend.auth.database import get_session_factory
        from backend.repositories.work_repository import WorkRepository

        org = _org(db)
        _seed_fact_product(db, org.id)
        sf = get_session_factory()

        # ---- 1. 一句话 brief → 建 run + 生成作品 ----
        brain = _one_shot_brain()
        out = run_one_shot_brief(
            db, "u1", org.id, "帮我给烟台苹果做一条抖音带货短视频",
            session_factory=lambda: sf(), ai_generate=brain)
        assert out["run_status"] == "completed", out["summary"]
        work_id = out["work_id"]
        conv_id = out["conversation_id"]
        assert work_id and conv_id
        assert brain.calls["n"] >= 5  # 真实走了多步模型调用

        # ---- 2. 新 session 重读：真实持久化验证 ----
        s2 = sf()
        try:
            w = WorkRepository(s2).get(work_id, org.id)
            assert w is not None
            data = json.loads(w.data_json)
        finally:
            s2.close()
        for k in ("strategy", "creative", "storyboard",
                  "shooting_list", "copy"):
            assert isinstance(data.get(k), dict) and data[k], k
        assert not verify_persistence(db, work_id, org.id,
                                      ["strategy", "creative", "storyboard",
                                       "shooting_list", "copy"])

        # ---- 3. 创建 run + 写入 plan ----
        task_id = create_agent_run(
            db, "u1", org.id, work_id=work_id,
            brief_json={"goal": "复核产品事实并补一条创意"})
        plan = {"steps": [
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products",
             "params": {"keyword": "苹果"}},
            {"tool_id": "generate_ideas",
             "params": {"brief": "给烟台苹果做抖音带货短视频"}},
        ]}
        v = set_run_plan(db, task_id, org.id, plan)
        assert v.ok

        # ---- 4. 真实 execute_run_steps：真实步骤循环 + 真实 adapters，
        #        仅模型经 fake 注入（ToolDeps 包装） ----
        from backend.services.agent.adapters import (
            run_tool as _real_run_tool, ToolDeps)
        from backend.services.agent import runner as _runner_mod
        ideas_brain = _ideas_brain()
        tool_calls = []
        tool_data = {}

        def _with_fake_model(deps, tool_id, params, confirmed=False):
            tool_calls.append(tool_id)
            fake_deps = ToolDeps(
                db=deps.db, identity=deps.identity,
                ai_generate=ideas_brain, model=None)
            res = _real_run_tool(fake_deps, tool_id, params,
                                 confirmed=confirmed)
            tool_data[tool_id] = res.data
            return res

        monkeypatch.setattr(_runner_mod, "run_tool", _with_fake_model)
        res = execute_run_steps(db, task_id, lambda: False)
        assert res["run_status"] == "completed", res
        assert tool_calls == ["query_facts", "query_products",
                              "generate_ideas"]

        # ---- 5. 真实步骤落盘：AgentStep 行 + 会话 tool 消息 ----
        steps = (db.query(AgentStep).filter_by(task_id=task_id)
                   .order_by(AgentStep.id).all())
        assert len(steps) == 3
        assert all(s.status == "succeeded" for s in steps)
        assert all(s.duration_ms is not None for s in steps)
        from backend.ai_tasks.repository import AITaskRepository
        task = AITaskRepository(db).get(task_id)
        run_conv_id = (task.payload_json or {}).get("conversation_id")
        assert run_conv_id
        roles = [m.role for m in list_messages(db, run_conv_id, org.id)]
        assert "tool" in roles
        # 工具真实读到了种子数据（真实 adapter + 真实 DB 行）
        facts = tool_data["query_facts"]["facts"]
        assert any("现摘现发" in f["content"] for f in facts)
        prods = tool_data["query_products"]["products"]
        assert any(p["name"] == "烟台苹果" for p in prods)
        ideas = tool_data["generate_ideas"]["ideas"]
        assert len(ideas) == 2 and ideas[0]["title"] == "果园现摘"

        # ---- 6. 对话局部修改：只改 copy.title ----
        # （一句话 run 的会话在作品建成前创建，未绑定 work；此处按真实
        #   前端流程新建绑定该作品的会话）
        from backend.services.agent import create_conversation
        mod_conv = create_conversation(db, org.id, "u1",
                                       work_id=work_id, brief={})
        before = json.loads(WorkRepository(db).get(
            work_id, org.id).data_json)
        plan_mod = {"target_kind": "artifact", "target": "copy",
                    "operation": "rewrite",
                    "params": {"instruction": "标题更有吸引力"},
                    "scope": "local", "touches_confirmed_fact": False}
        mbrain = _ModifyBrain(
            plan_mod, {"title": "果园现摘！烟台苹果脆甜来袭"})
        mod_out = apply_modification(
            db, "u1", org.id, work_id, mod_conv.id,
            "SCN:title 把文案标题改得更有吸引力", ai_generate=mbrain)
        assert mod_out["status"] == "completed", mod_out
        after = json.loads(WorkRepository(db).get(
            work_id, org.id).data_json)
        assert after["copy"]["title"] == "果园现摘！烟台苹果脆甜来袭"
        # 无关 artifact 零变化（局部修改保护）
        for k in ("strategy", "creative", "storyboard",
                  "shooting_list"):
            assert after[k] == before[k], k
        snap_no = mod_out["snapshot_version_no"]
        assert snap_no >= 1

        # ---- 7. 版本恢复：一键回到修改前 ----
        r = restore_modification(db, "u1", org.id, work_id, snap_no)
        assert r["restored"] is True
        restored = json.loads(WorkRepository(db).get(
            work_id, org.id).data_json)
        assert restored["copy"]["title"] == before["copy"]["title"]
        assert restored == before

        # ---- 8. 导出：export-json（含版本历史） ----
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.auth.deps import get_current_user
        from backend.auth.database import get_db
        from backend.auth.models import User as AuthUser
        from backend.security.rate_limit import reset_rate_limit_buckets
        reset_rate_limit_buckets()

        def _override_user():
            return AuthUser(id="u1", email="u1@test.local",
                            password_hash="x")

        def _override_db():
            s = sf()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db] = _override_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/v1/works/%s/export-json" % work_id)
        finally:
            app.dependency_overrides.clear()
            reset_rate_limit_buckets()
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 0, body
        exp = body["data"]
        assert exp["work"]["id"] == work_id
        assert isinstance(exp["work"]["data"].get("copy"), dict)
        assert any(v["version_no"] == snap_no
                   for v in exp["versions"])


# ================= 故障演练 =================

def _make_run(db, org_id, plan, work_id=None):
    from backend.services.agent import create_agent_run, set_run_plan
    task_id = create_agent_run(
        db, "u1", org_id, work_id=work_id,
        brief_json={"goal": "故障演练"})
    set_run_plan(db, task_id, org_id, plan)
    return task_id


def _plan(*steps):
    return {"steps": list(steps)}


class TestFaultDrills:
    def test_model_timeout_fast_fail(self, db, monkeypatch):
        """模型超时 → 步骤失败、当次调用只 1 次（无重试风暴）、错误落盘。"""
        import time
        from backend.services.agent import execute_run_steps
        from backend.services.agent.runner import AgentStep
        org = _org(db)
        task_id = _make_run(db, org.id, _plan(
            {"tool_id": "query_facts", "params": {}}))
        calls = []

        def timeout_tool(deps, tool_id, params, confirmed=False):
            calls.append(tool_id)
            raise TimeoutError("模型网关超时 30s")

        monkeypatch.setattr(
            "backend.services.agent.runner.run_tool", timeout_tool)
        t0 = time.time()
        out = execute_run_steps(db, task_id, lambda: False)
        dt = time.time() - t0
        assert out["run_status"] == "failed"
        assert calls == ["query_facts"]  # 无重试
        assert dt < 10  # 快速失败，无固定延迟
        row = db.query(AgentStep).filter_by(task_id=task_id).one()
        assert row.status == "failed"
        assert "TimeoutError" in (row.error or "")

    def test_429_hard_stop_reverify(self, db):
        """复核：编排层撞 429 → 硬停，之后零新模型调用（phase5 主用例）。"""
        from backend.services.agent import run_one_shot_brief
        from backend.auth.database import get_session_factory
        org = _org(db)
        sf = get_session_factory()
        seen = []

        def fake(prompt, feedback=None):
            seen.append(prompt)
            if "营销策略师" in prompt:
                raise RuntimeError("HTTP 429 Too Many Requests")
            if "brief 解析器" in prompt:
                return _base_brief()
            raise AssertionError("不应走到这里: %s" % prompt[:40])

        out = run_one_shot_brief(
            db, "u1", org.id, "给烟台苹果做视频",
            session_factory=lambda: sf(), ai_generate=fake)
        assert out["run_status"] in ("partial", "failed")
        assert any("429" in w for w in out["summary"]["warnings"])
        # 硬停后无分镜/文案/创意调用
        blob = " ".join(seen)
        assert "分镜师" not in blob and "平台文案写手" not in blob

    def test_5xx_no_indiscriminate_retry(self, db):
        """5xx → 直接失败，不无差别重试（调用恰好 1 次）。"""
        from backend.services.agent import run_one_shot_brief
        from backend.auth.database import get_session_factory
        org = _org(db)
        sf = get_session_factory()
        calls = []

        def fake500(prompt, feedback=None):
            calls.append(1)
            raise RuntimeError("500 Internal Server Error")

        out = run_one_shot_brief(
            db, "u1", org.id, "给烟台苹果做视频",
            session_factory=lambda: sf(), ai_generate=fake500)
        assert out["run_status"] == "failed"
        assert len(calls) == 1

    def test_optional_tool_failure_partial(self, db, monkeypatch):
        """可选步骤失败 → partial；必需步骤失败 → failed。"""
        from backend.services.agent import execute_run_steps
        from backend.services.agent.adapters import ToolResult
        from backend.services.agent.runner import AgentStep
        org = _org(db)

        def flaky(deps, tool_id, params, confirmed=False):
            if tool_id == "query_products":
                return ToolResult(status="failed", error_code="internal",
                                  error_message="模拟失败")
            return ToolResult(status="ok", data={})

        monkeypatch.setattr(
            "backend.services.agent.runner.run_tool", flaky)

        t1 = _make_run(db, org.id, _plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {},
             "optional": True}))
        out = execute_run_steps(db, t1, lambda: False)
        assert out["run_status"] == "partial"
        statuses = {s.tool_id: s.status for s in
                    db.query(AgentStep).filter_by(task_id=t1).all()}
        assert statuses == {"query_facts": "succeeded",
                            "query_products": "failed"}

        t2 = _make_run(db, org.id, _plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}}))
        out2 = execute_run_steps(db, t2, lambda: False)
        assert out2["run_status"] == "failed"
        assert out2["failed_step"] == 1

    def test_duplicate_submit_idempotent(self, db):
        """同幂等键同体重复提交 → 同一 task_id，不重复建任务。"""
        from starlette.requests import Request
        from backend.core.idempotency import idempotency_begin
        from backend.services.agent import create_agent_run
        from backend.ai_tasks.repository import AITaskRepository
        org = _org(db)

        def _req(key):
            return Request({"type": "http", "headers": [
                (b"idempotency-key", key.encode())]})

        body = {"org_id": org.id, "brief": {"goal": "幂等建 run"}}
        key = "k-phase8-drill-1"

        def _create_once():
            idem = idempotency_begin("agent-runs", _req(key), body,
                                     scope=org.id, org_id=org.id)
            if idem.replay is not None:
                return idem.replay
            try:
                task_id = create_agent_run(
                    db, "u1", org.id, brief_json={"goal": "幂等建 run"})
            except Exception:
                idem.abort()
                raise
            return idem.finish({"task_id": task_id})

        r1 = _create_once()
        r2 = _create_once()
        assert r1["task_id"] == r2["task_id"]
        assert r2.get("idempotent_replay") is True
        repo = AITaskRepository(db)
        assert repo.get(r1["task_id"]) is not None
        n = sum(1 for t in repo.list_by_org(org.id)
                if t.task_type == "agent_run")
        assert n == 1

    def test_user_cancel_mid_run(self, db, monkeypatch):
        """用户中途取消 → cancelled；已成功步骤保留，未执行步骤不再跑。"""
        from backend.services.agent import execute_run_steps
        from backend.services.agent.adapters import ToolResult
        from backend.services.agent.runner import AgentStep
        org = _org(db)
        task_id = _make_run(db, org.id, _plan(
            {"tool_id": "query_facts", "params": {}},
            {"tool_id": "query_products", "params": {}},
            {"tool_id": "query_facts", "params": {"status": "all"}}))
        calls = []

        def ok_tool(deps, tool_id, params, confirmed=False):
            calls.append(tool_id)
            return ToolResult(status="ok", data={})

        monkeypatch.setattr(
            "backend.services.agent.runner.run_tool", ok_tool)
        state = {"cancel": False}
        orig_execute = execute_run_steps

        def counting_cancel():
            # 第一步完成后取消
            if len(calls) >= 1:
                state["cancel"] = True
            return state["cancel"]

        from backend.ai_tasks.queue import _TaskCancelled
        with pytest.raises(_TaskCancelled):
            orig_execute(db, task_id, counting_cancel)
        from backend.ai_tasks.repository import AITaskRepository
        task = AITaskRepository(db).get(task_id)
        assert task.payload_json["run_status"] == "cancelled"
        assert calls == ["query_facts"]  # 取消后不再执行后续步骤
        statuses = [s.status for s in
                    db.query(AgentStep).filter_by(task_id=task_id)
                    .order_by(AgentStep.id).all()]
        assert statuses[0] == "succeeded"
        # queue 落库语义：绝不误报 succeeded
        assert task.status != "succeeded"

    def test_concurrent_edit_conflict(self, db):
        """并发编辑：stale expected_version → 乐观锁冲突，不静默覆盖。"""
        from backend.services.agent.adapters import (
            run_tool, ToolDeps, ToolResult)
        from backend.services.agent.context import AgentIdentity
        from backend.repositories.work_repository import WorkRepository
        org = _org(db)
        w = WorkRepository(db).create(
            org_id=org.id, owner_id="u1", title="并发作品",
            data={"copy": {"title": "v1"}}, status="draft")
        db.commit()
        db.refresh(w)
        v0 = w.version
        deps = ToolDeps(
            db=db,
            identity=AgentIdentity(user_id="u1", org_id=org.id,
                                  role="owner"),
            ai_generate=None)

        r1 = run_tool(deps, "patch_work",
                      {"work_id": w.id, "expected_version": v0,
                       "data_patch": {"copy": {"title": "v2"}}},
                      confirmed=True)
        assert r1.status == "ok", r1.error_message
        assert r1.data["version"] == v0 + 1

        # 另一路用旧版本号提交 → 冲突失败
        r2 = run_tool(deps, "patch_work",
                      {"work_id": w.id, "expected_version": v0,
                       "data_patch": {"copy": {"title": "v3-stale"}}},
                      confirmed=True)
        assert r2.status == "failed"
        assert r2.error_code in ("conflict", "version_conflict",
                                 "optimistic_lock")
        # 数据未被 stale 写入覆盖
        cur = json.loads(WorkRepository(db).get(w.id, org.id).data_json)
        assert cur["copy"]["title"] == "v2"
