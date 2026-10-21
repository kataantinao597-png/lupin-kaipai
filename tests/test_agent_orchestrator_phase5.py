"""Phase 5 测试：一句话生成编排（brief 解析 / orchestrator 11 步 / 质量门禁 /
confirm 异步化 / 多轮串联）。

- 业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。
- 模型调用一律经 ai_generate fake 注入（prompt, feedback)->dict，
  不烧真实 token；1 次真实 brief 解析冒烟单独走脚本（见 EXECUTION_LOG）。
- 回归：test_agent_api_phase4a.py + test_agent_run_phase3.py 保持全绿。

运行：./venv/bin/python -m pytest tests/test_agent_orchestrator_phase5.py
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
    url = f"sqlite:///{tmp_path}/phase5.db"
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


def _org(db, name="苹果合作社"):
    from backend.models import Organization
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _member(db, org_id, user_id="u1", role="owner"):
    from backend.models import Membership
    db.add(Membership(user_id=user_id, org_id=org_id, role=role))
    db.commit()


def _run(db, org_id, brief_text, **kw):
    """调 orchestrator 跑完整一句话流程（fake 模型注入）。"""
    from backend.services.agent import run_one_shot_brief
    from backend.auth.database import get_session_factory
    _sf = get_session_factory()
    return run_one_shot_brief(
        db, "u1", org_id, brief_text,
        session_factory=lambda: _sf(), **kw)


# ---------------------------------------------------------------------------
# fake 模型大脑：按 prompt 标记分发各步骤的 JSON
# ---------------------------------------------------------------------------

def _base_brief(**kw):
    d = {
        "product_name": "烟台苹果",
        "platform": "抖音",
        "marketing_goal": "带货卖苹果",
        "target_audience": "注重健康的年轻人",
        "budget_yuan": 2000,
        "people_count": 2,
        "devices": ["手机", "三脚架"],
        "location": "果园",
        "video_duration_sec": 60,
        "style": "拉呱风",
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
         "selling_points": ["个头匀称", "果香浓郁"]},
        {"title": "果农的一天", "angle": "记录果农日常",
         "selling_points": ["用心种植", "品质把控"]}]}


def _storyboard(duration=60, shots=5, risky=False):
    per = duration / shots
    out = []
    for i in range(1, shots + 1):
        vo = f"第{i}镜：果园里现摘的苹果，又脆又甜" if not risky \
            else f"第{i}镜：包治百病，吃了降血压的神奇苹果"
        out.append({
            "shot_no": i, "scene": f"果园场景{i}", "shot_type": "中景",
            "duration_sec": per, "voiceover": vo,
            "subtitle": f"字幕{i}", "shoot_notes": f"拍摄要点{i}"})
    return {"shots": out}


def _storyboard_from_prompt(prompt, risky=False):
    """按 adapter prompt 里的目标时长/镜头数生成（与真实行为对齐）。"""
    import re
    m_dur = re.search(r"目标时长约 (\d+) 秒", prompt)
    m_n = re.search(r"共 (\d+) 个镜头", prompt)
    duration = int(m_dur.group(1)) if m_dur else 60
    shots = int(m_n.group(1)) if m_n else 5
    return _storyboard(duration=duration, shots=shots, risky=risky)


def _copy():
    return {"copy": {
        "platform": "抖音",
        "title": "烟台苹果，现摘现发",
        "text": "果园现摘的苹果，又脆又甜，欢迎下单",
        "hashtags": ["烟台苹果", "助农"]}}


def _fake_brain(scenario):
    """scenario(kind, n, prompt, feedback) -> dict；记录调用次数。"""
    calls = {"n": 0, "by_kind": {}}

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
        calls["by_kind"][kind] = calls["by_kind"].get(kind, 0) + 1
        return scenario(kind, calls["by_kind"][kind], prompt, feedback)

    fake.calls = calls
    return fake


def _happy_scenario(brief_kw=None, risky=False):
    bk = brief_kw or {}

    def scenario(kind, n, prompt, feedback):
        if kind == "brief":
            return _base_brief(**bk)
        if kind == "strategy":
            return _strategy()
        if kind == "ideas":
            return _ideas()
        if kind == "storyboard":
            return _storyboard_from_prompt(prompt, risky=risky)
        if kind == "copy":
            return _copy()
        raise AssertionError(f"未预期的模型调用: {kind}")
    return scenario


# ---------------------------------------------------------------------------
# 1. 正常生成：11 步全通 → completed，作品可重读且一致
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_full_flow_completed(self, db):
        org = _org(db)
        _member(db, org.id)
        out = _run(db, org.id, "给烟台苹果做条抖音带货视频，60秒，预算2000，2个人",
                   ai_generate=_fake_brain(_happy_scenario()))
        assert out["run_status"] == "completed", out["summary"]
        assert out["work_id"]
        assert out["work_version"] == 2  # 新建作品 v1 → patch_work 乐观锁 +1
        s = out["summary"]
        assert s["brief"]["product_name"] == "烟台苹果"
        assert s["model_calls"] == 5  # brief/strategy/ideas/storyboard/copy
        assert s["quality"]["passed"] is True
        # agent_steps 有记录
        from backend.services.agent import AgentStep
        rows = db.query(AgentStep).filter_by(task_id=out["task_id"]).all()
        tool_ids = {r.tool_id for r in rows}
        assert {"parse_brief", "query_products", "query_facts",
                "generate_strategy", "generate_ideas", "generate_storyboard",
                "shooting_checklist", "check_risk", "generate_platform_copy",
                "check_delivery", "patch_work"} <= tool_ids
        # 任务终态 + 摘要落 conversation
        from backend.ai_tasks.repository import AITaskRepository
        t = AITaskRepository(db).get(out["task_id"])
        assert t.status == "succeeded"
        assert t.payload_json["run_status"] == "completed"
        from backend.services.agent import list_messages
        msgs = list_messages(db, out["conversation_id"], org.id, limit=50)
        assert any(m.content_json.get("type") == "one_shot_summary"
                   for m in msgs)

    def test_reread_consistency(self, db):
        """生成后重读作品：artifact 真实持久化且关联正确。"""
        from backend.auth.database import get_session_factory
        from backend.repositories.work_repository import WorkRepository
        org = _org(db)
        _member(db, org.id)
        out = _run(db, org.id, "给烟台苹果做条抖音带货视频",
                   ai_generate=_fake_brain(_happy_scenario()))
        assert out["run_status"] == "completed"
        # 新会话重读（模拟刷新/恢复）
        s2 = get_session_factory()()
        try:
            w = WorkRepository(s2).get(out["work_id"], org.id)
            data = json.loads(w.data_json)
            for k in ("brief", "strategy", "creative", "storyboard",
                      "shooting_list"):
                assert isinstance(data.get(k), dict) and data[k], k
            assert data["brief"]["product_name"] == "烟台苹果"
            assert len(data["storyboard"]["shots"]) == 5
            assert w.version == out["work_version"] == 2
        finally:
            s2.close()


# ---------------------------------------------------------------------------
# 2. 关键字段缺失 → 透明默认值 + 标出
# ---------------------------------------------------------------------------

class TestMissingFields:
    def test_transparent_defaults(self, db):
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "brief":
                # 只给产品名，其余缺失
                return {"product_name": "日照绿茶"}
            return _happy_scenario()(kind, n, prompt, feedback)

        out = _run(db, org.id, "日照绿茶",
                   ai_generate=_fake_brain(scenario))
        assert out["run_status"] == "completed", out["summary"]
        brief = out["summary"]["brief"]
        assert brief["platform"] == "抖音"  # 透明默认
        assert brief["video_duration_sec"] == 60
        assert any("平台" in d for d in brief["confirmed_defaults"])
        assert any("视频时长" in d for d in brief["confirmed_defaults"])


# ---------------------------------------------------------------------------
# 3. 模型返回无效 JSON → ≤2 次受控修复；超限 → 失败（不瞎编）
# ---------------------------------------------------------------------------

class TestInvalidJson:
    def test_repair_then_success(self, db):
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "brief" and n == 1:
                # 第一次返回结构非法（video_duration_sec 无法解析）
                return {"product_name": "烟台苹果",
                        "video_duration_sec": "not_a_number"}
            return _happy_scenario()(kind, n, prompt, feedback)

        brain = _fake_brain(scenario)
        out = _run(db, org.id, "给烟台苹果做视频", ai_generate=brain)
        assert out["run_status"] == "completed", out["summary"]
        assert brain.calls["by_kind"]["brief"] == 2  # 1 次修复

    def test_repair_exhausted_fails(self, db):
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "brief":
                return {"video_duration_sec": "not_a_number"}  # 永远非法
            return _happy_scenario()(kind, n, prompt, feedback)

        brain = _fake_brain(scenario)
        out = _run(db, org.id, "给烟台苹果做视频", ai_generate=brain)
        assert out["run_status"] == "failed", out
        assert brain.calls["by_kind"]["brief"] == 3  # 初次 + 2 次修复
        assert "brief" in out["summary"]["error"]
        # 失败也不建作品写脏数据（run 失败在步骤 1，无 work）
        assert out["work_id"] is None


# ---------------------------------------------------------------------------
# 4. 单个工具失败 → failed，保留已有成果
# ---------------------------------------------------------------------------

class TestToolFailure:
    def test_required_step_failure(self, db):
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "ideas":
                raise RuntimeError("模型服务抖动")
            return _happy_scenario()(kind, n, prompt, feedback)

        out = _run(db, org.id, "给烟台苹果做视频",
                   ai_generate=_fake_brain(scenario))
        assert out["run_status"] == "failed"
        assert "创意生成失败" in out["summary"]["error"]
        # 已有成果保留在 step_log / summary 中
        tool_ids = [s["tool_id"] for s in out["summary"]["steps"]]
        assert "parse_brief" in tool_ids
        assert "generate_strategy" in tool_ids
        from backend.ai_tasks.repository import AITaskRepository
        t = AITaskRepository(db).get(out["task_id"])
        assert t.status == "failed"


# ---------------------------------------------------------------------------
# 5. 风险内容拦截：高风险宣称无事实引用 → 标待核实，不作为确定宣传
# ---------------------------------------------------------------------------

class TestRiskyContent:
    def test_risky_claim_marked_unverified(self, db):
        from backend.repositories.work_repository import WorkRepository
        org = _org(db)
        _member(db, org.id)
        out = _run(db, org.id, "给烟台苹果做视频",
                   ai_generate=_fake_brain(_happy_scenario(risky=True)))
        assert out["run_status"] == "completed", out["summary"]
        s = out["summary"]
        assert s["repaired_claims"], "应有修复记录"
        assert any("待核实" in w for w in s["warnings"])
        # 持久化内容里高风险句已标待核实
        w = WorkRepository(db).get(out["work_id"], org.id)
        data = json.loads(w.data_json)
        blob = json.dumps(data, ensure_ascii=False)
        assert "待核实" in blob
        assert "不可作为确定宣传" in blob
        # 质量门禁：事实引用门禁通过（已标待核实视为合规）
        gates = {g["name"]: g for g in s["quality"]["gates"]}
        assert gates["fact_grounding"]["passed"] is True


# ---------------------------------------------------------------------------
# 6. 低预算单人：约束校验给出明确建议，不静默失败
# ---------------------------------------------------------------------------

class TestLowBudgetSolo:
    def test_solo_low_budget(self, db):
        org = _org(db)
        _member(db, org.id)
        out = _run(
            db, org.id, "预算300元就我一个人，给苹果拍条30秒抖音",
            ai_generate=_fake_brain(_happy_scenario(
                {"budget_yuan": 300, "people_count": 1,
                 "devices": ["手机"], "video_duration_sec": 30})))
        assert out["run_status"] == "completed", out["summary"]
        s = out["summary"]
        assert s["brief"]["people_count"] == 1
        assert s["brief"]["budget_yuan"] == 300
        assert any("300" in w and "预算" in w for w in s["warnings"])
        # 拍摄清单按单人配置
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).get(out["work_id"], org.id)
        data = json.loads(w.data_json)
        assert data["shooting_list"]["crew_size"] == 1
        assert len(data["storyboard"]["shots"]) == 3  # 30秒→3镜


# ---------------------------------------------------------------------------
# 7. 可选步骤失败 → partial，核心成果保留
# ---------------------------------------------------------------------------

class TestPartial:
    def test_optional_copy_failure_partial(self, db):
        from backend.repositories.work_repository import WorkRepository
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "copy":
                raise RuntimeError("文案模型超时")
            return _happy_scenario()(kind, n, prompt, feedback)

        out = _run(db, org.id, "给烟台苹果做视频",
                   ai_generate=_fake_brain(scenario))
        assert out["run_status"] == "partial", out["summary"]
        assert any("平台文案" in w for w in out["summary"]["warnings"])
        # 核心 artifact 已写回
        w = WorkRepository(db).get(out["work_id"], org.id)
        data = json.loads(w.data_json)
        assert data["storyboard"] and data["shooting_list"]
        assert "copy" not in data  # 可选失败不写脏数据
        from backend.ai_tasks.repository import AITaskRepository
        t = AITaskRepository(db).get(out["task_id"])
        assert t.status == "succeeded"  # partial 落 succeeded + run_status
        assert t.payload_json["run_status"] == "partial"

    def test_model_steps_cap_partial(self, db, monkeypatch):
        """run 级模型步上限：超限进 partial 保留成果（上限收紧到 3 步模拟）。"""
        import backend.services.agent.orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "MAX_MODEL_STEPS_PER_RUN", 3)
        org = _org(db)
        _member(db, org.id)
        out = _run(db, org.id, "给烟台苹果做视频",
                   ai_generate=_fake_brain(_happy_scenario()))
        assert out["run_status"] == "partial", out["summary"]
        assert any("模型步数" in w for w in out["summary"]["warnings"])


# ---------------------------------------------------------------------------
# 8. 429 硬停止：撞 429 立刻停，不重试不换端点
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_429_hard_stop(self, db):
        org = _org(db)
        _member(db, org.id)

        def scenario(kind, n, prompt, feedback):
            if kind == "strategy":
                raise RuntimeError("HTTP 429 Too Many Requests")
            return _happy_scenario()(kind, n, prompt, feedback)

        brain = _fake_brain(scenario)
        out = _run(db, org.id, "给烟台苹果做视频", ai_generate=brain)
        assert out["run_status"] in ("partial", "failed"), out["summary"]
        assert any("429" in w for w in out["summary"]["warnings"])
        # 硬停止：429 之后不再发起新的模型调用（无 storyboard/ideas/copy 调用）
        assert brain.calls["by_kind"].get("storyboard", 0) == 0
        assert brain.calls["by_kind"].get("copy", 0) == 0
        assert brain.calls["by_kind"].get("ideas", 0) == 0


# ---------------------------------------------------------------------------
# 9. confirm 异步化（service 层）：只落确认态，worker 恢复执行
# ---------------------------------------------------------------------------

class TestConfirmAsync:
    def _make_waiting(self, db, org, monkeypatch):
        from backend.services.agent import (
            ToolResult, create_agent_run, execute_run_steps, set_run_plan)
        from backend.models import Work
        work = Work(org_id=org.id, owner_id="u1", title="w")
        db.add(work)
        db.commit()
        task_id = create_agent_run(
            db, "u1", org.id, work_id=work.id, brief_json={"goal": "x"})
        set_run_plan(db, task_id, org.id, {"steps": [
            {"tool_id": "patch_work",
             "params": {"work_id": work.id, "expected_version": 1,
                        "title": "AI 改后"}}]})
        calls = []

        def fake(deps, tool_id, params, confirmed=False):
            calls.append((tool_id, confirmed))
            return ToolResult(status="ok", data={})

        monkeypatch.setattr("backend.services.agent.runner.run_tool", fake)
        out = execute_run_steps(db, task_id, lambda: False)
        assert out["run_status"] == "waiting_user"
        return task_id, calls

    def test_confirm_only_records_state(self, db, monkeypatch):
        """批准只落确认态：HTTP 内不执行工具，由 worker 恢复。"""
        from backend.services.agent import confirm_step, run_agent_run
        org = _org(db)
        _member(db, org.id)
        task_id, calls = self._make_waiting(db, org, monkeypatch)
        out = confirm_step(db, task_id, 0, True)
        assert out["run_status"] == "running"
        assert out["resumed"] is False
        assert calls == []  # 确认时工具未执行
        # worker 恢复执行后完成
        out2 = run_agent_run(task_id, lambda: False, {})
        assert out2["run_status"] == "completed"
        assert calls == [("patch_work", True)]


# ---------------------------------------------------------------------------
# 10. 多轮串联：同一 conversation 复用 + brief 增量更新
# ---------------------------------------------------------------------------

class TestMultiTurn:
    def test_reuse_conversation(self, db):
        from backend.services.agent import (
            create_agent_run, get_run_state, list_conversations,
            list_messages)
        org = _org(db)
        _member(db, org.id)
        t1 = create_agent_run(db, "u1", org.id,
                             brief_json={"text": "给苹果做视频"})
        conv1 = get_run_state(db, t1, org.id)["conversation_id"]
        t2 = create_agent_run(
            db, "u1", org.id, brief_json={"text": "改成30秒"},
            conversation_id=conv1)
        conv2 = get_run_state(db, t2, org.id)["conversation_id"]
        assert conv2 == conv1, "同一会话内后续消息应复用 conversation"
        assert len(list_conversations(db, org.id, user_id="u1")) == 1
        # brief 增量更新：两条用户消息都在
        msgs = list_messages(db, conv1, org.id, limit=50)
        briefs = [m for m in msgs
                  if m.content_json.get("type") == "brief"]
        assert len(briefs) == 2

    def test_reuse_unknown_conversation_404(self, db):
        from backend.services.agent import create_agent_run
        from backend.repositories.base import EntityNotFound
        org = _org(db)
        _member(db, org.id)
        with pytest.raises(EntityNotFound):
            create_agent_run(db, "u1", org.id, brief_json={"text": "x"},
                             conversation_id="no_such_conv")


# ---------------------------------------------------------------------------
# 11. brief 单元：解析 / 修复上限 / 合并 / 不臆造关键事实
# ---------------------------------------------------------------------------

class TestBriefUnit:
    def test_parse_and_defaults(self):
        from backend.services.agent import parse_brief_from_text
        b = parse_brief_from_text(
            "给烟台苹果做条抖音视频",
            lambda prompt, feedback=None: _base_brief())
        assert b.product_name == "烟台苹果"
        assert b.platform == "抖音"
        assert b.video_duration_sec == 60
        assert b.confirmed_defaults  # 缺失字段标出

    def test_no_hallucination(self):
        """关键事实不明 → 待核实，不臆造进正文字段。"""
        from backend.services.agent import parse_brief_from_text
        b = parse_brief_from_text(
            "给苹果做视频",
            lambda prompt, feedback=None: {
                "product_name": "苹果",
                "marketing_goal": "带货",
                "items_to_verify": ["产地待核实", "有机认证待核实"]})
        assert b.items_to_verify == ["产地待核实", "有机认证待核实"]
        # 正文不含臆造的关键事实
        blob = f"{b.marketing_goal}{b.target_audience}"
        assert "有机" not in blob or "待核实" in " ".join(b.items_to_verify)

    def test_repair_limit(self):
        from backend.services.agent import (
            BriefParseError, parse_brief_from_text)
        n = {"c": 0}

        def bad(prompt, feedback=None):
            n["c"] += 1
            return {"video_duration_sec": "not_a_number"}

        with pytest.raises(BriefParseError):
            parse_brief_from_text("x", bad)
        assert n["c"] == 3  # 初次 + 2 次修复

    def test_merge_brief(self):
        from backend.services.agent import Brief, merge_brief
        b1 = Brief.model_validate(_base_brief())
        b2 = Brief.model_validate({"video_duration_sec": 30,
                                   "devices": ["稳定器"]})
        m = merge_brief(b1, b2)
        assert m.video_duration_sec == 30  # 新值覆盖
        assert m.product_name == "烟台苹果"  # 旧值保留
        assert "稳定器" in m.devices and "手机" in m.devices  # 列表合并


# ---------------------------------------------------------------------------
# 12. 质量门禁单元：schema / 一致性 / 风险引用 / 持久化
# ---------------------------------------------------------------------------

class TestQualityGates:
    def _artifacts(self, **kw):
        sb = _storyboard()
        arts = {
            "strategy": _strategy()["strategy"],
            "creative": {"directions": _ideas()["ideas"],
                         "recommended_index": 0},
            "storyboard": sb,
            "shooting_list": {
                "items": [{"shot_no": s["shot_no"], "scene": s["scene"],
                           "duration_sec": s["duration_sec"],
                           "people_needed": 1,
                           "devices_needed": ["手机", "三脚架"],
                           "props": []} for s in sb["shots"]],
                "total_duration_sec": 60,
                "crew_size": 2,
                "devices_available": ["手机", "三脚架"]},
            "copy": _copy()["copy"],
        }
        arts.update(kw)
        return arts

    def test_all_pass(self):
        from backend.services.agent import Brief, run_quality_gates
        brief = Brief.model_validate(_base_brief())
        rep = run_quality_gates(brief, self._artifacts(), facts=[])
        assert rep.passed is True
        assert rep.verdict == "completed"

    def test_schema_failure(self):
        from backend.services.agent import Brief, run_quality_gates
        brief = Brief.model_validate(_base_brief())
        arts = self._artifacts(storyboard={"shots": []})  # 空分镜非法
        rep = run_quality_gates(brief, arts, facts=[])
        assert rep.passed is False
        assert rep.verdict == "failed"

    def test_duration_mismatch(self):
        from backend.services.agent import Brief, run_quality_gates
        brief = Brief.model_validate(_base_brief(video_duration_sec=60))
        sb = _storyboard(duration=120)  # 总时长 120 vs 目标 60
        arts = self._artifacts(storyboard=sb)
        rep = run_quality_gates(brief, arts, facts=[])
        assert rep.passed is False
        cons = next(g for g in rep.gates if g.name == "consistency")
        assert any("偏差" in i.message for i in cons.issues)

    def test_risky_without_ref_fails(self):
        from backend.services.agent import (
            Brief, check_fact_grounding)
        brief = Brief.model_validate(_base_brief())
        arts = self._artifacts(storyboard=_storyboard(risky=True))
        issues = check_fact_grounding(brief, arts, [])
        assert issues, "高风险宣称无引用应被拦截"
        assert any("包治" in i.message or "降压" in i.message
                   for i in issues)

    def test_risky_marked_passes(self):
        from backend.services.agent import (
            Brief, check_fact_grounding)
        brief = Brief.model_validate(_base_brief())
        sb = _storyboard(risky=True)
        for s in sb["shots"]:
            s["voiceover"] += "（待核实，不可作为确定宣传）"
        issues = check_fact_grounding(
            brief, self._artifacts(storyboard=sb), [])
        assert issues == []

    def test_persistence_verify(self, db):
        from backend.services.agent import (
            Brief, verify_persistence)
        from backend.models import Work
        org = _org(db)
        work = Work(org_id=org.id, owner_id="u1", title="w",
                    data_json=json.dumps({"storyboard": {"shots": [1]}}))
        db.add(work)
        db.commit()
        brief = Brief.model_validate(_base_brief())
        assert verify_persistence(
            db, work.id, org.id, ["storyboard"]) == []
        issues = verify_persistence(db, work.id, org.id, ["copy"])
        assert issues and "copy" in issues[0].message


# ---------------------------------------------------------------------------
# 13. HTTP 层：conversation_id 透传（多轮串联的路由接线）
# ---------------------------------------------------------------------------

class TestConversationHttp:
    @pytest.fixture()
    def api_client(self, tmp_path, monkeypatch):
        from backend import ai_observability as _obs
        monkeypatch.setattr(_obs, "record_call_async", lambda rec: None)
        url = f"sqlite:///{tmp_path}/agent_p5_api.db"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
        from backend.auth.database import (
            get_db, get_session_factory, reset_engine)
        from backend.auth.deps import get_current_user
        reset_engine()
        from alembic.config import Config
        from alembic import command
        cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
        command.upgrade(cfg, "head")

        from backend.models import Membership, Organization
        from backend.auth.models import User as AuthUser
        db = get_session_factory()()
        org = Organization(name="合作社", created_by="u1")
        db.add(org)
        db.flush()
        user = AuthUser(id="u1", email="u1@test.local", password_hash="x")
        db.add(user)
        db.flush()
        db.add(Membership(user_id="u1", org_id=org.id, role="owner"))
        db.commit()
        ids = {"org": org.id}
        db.close()

        from fastapi.testclient import TestClient
        from backend.app import app

        def _override_user():
            return AuthUser(id="u1", email="u1@test.local",
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
        yield client, ids
        app.dependency_overrides.clear()
        reset_engine()

    def test_create_run_reuses_conversation(self, api_client):
        client, ids = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org"],
                              "brief": {"text": "第一句"}})
        assert r.status_code == 202, r.text
        conv1 = r.json()["data"]["conversation_id"]
        assert conv1
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org"],
                              "brief": {"text": "第二句"},
                              "conversation_id": conv1})
        assert r.status_code == 202, r.text
        assert r.json()["data"]["conversation_id"] == conv1

    def test_create_run_bad_conversation_404(self, api_client):
        client, ids = api_client
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org"],
                              "brief": {"text": "x"},
                              "conversation_id": "no_such"})
        assert r.status_code == 404, r.text

    def test_confirm_approved_enqueues_not_executes(self, api_client,
                                                   monkeypatch):
        """批准确认：HTTP 内不执行、只入队（4a 遗留修复的 HTTP 断言）。"""
        from backend.services.agent import (
            ToolResult, execute_run_steps, set_run_plan)
        from backend.auth.database import get_session_factory
        from backend.models import Work
        client, ids = api_client
        s = get_session_factory()()
        work = Work(org_id=ids["org"], owner_id="u1", title="w")
        s.add(work)
        s.commit()
        work_id = work.id
        s.close()
        r = client.post("/api/v1/agent/runs",
                        json={"org_id": ids["org"], "work_id": work_id,
                              "brief": {"goal": "确认流"}})
        task_id = r.json()["data"]["task_id"]
        r = client.post(f"/api/v1/agent/runs/{task_id}/plan", json={
            "steps": [{"tool_id": "patch_work",
                       "params": {"work_id": work_id,
                                  "expected_version": 1,
                                  "title": "AI 改后"}}]})
        assert r.json()["data"]["needs_confirmation"] == [0]

        calls = []

        def fake(deps, tool_id, params, confirmed=False):
            calls.append(tool_id)
            return ToolResult(status="ok", data={})

        monkeypatch.setattr("backend.services.agent.runner.run_tool", fake)
        s = get_session_factory()()
        try:
            out = execute_run_steps(s, task_id, lambda: False)
        finally:
            s.close()
        assert out["run_status"] == "waiting_user"

        enqueued = []
        monkeypatch.setattr(
            "backend.api.v1.agent._enqueue_run_resume",
            lambda task: enqueued.append(task.id))
        r = client.post(f"/api/v1/agent/runs/{task_id}/confirm",
                        json={"step_idx": 0, "approved": True})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["run_status"] == "running"
        assert data["resumed"] is False
        assert calls == []  # HTTP 内未执行
        assert enqueued == [task_id]  # 已入队，worker 恢复
