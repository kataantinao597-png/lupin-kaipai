"""Phase 8 边界单元测试：brief / modify / orchestrator / quality_gates。

查漏补缺（任务书 §十二 Phase 8）：
- phase5 的 TestBriefUnit / TestQualityGates 覆盖了 happy path，本文件补边界：
  空输入、修复上限、透明默认值边缘、merge 语义、schema 严格模式、
  事实引用边界、保护规则阈值、版本快照上限、持久化失败路径、
  429 穿透、取消前置、_ModelCaller 模型步上限。

业务库 backend/data/lupin.db 只读，绝不写入/迁移：DB 用例一律 scratch 库。
模型调用一律 fake 注入，不烧真实 token。

运行：./venv/bin/python -m pytest tests/test_agent_boundary_phase8.py
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
    url = f"sqlite:///{tmp_path}/phase8_boundary.db"
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


def _org(db, name="边界合作社"):
    from backend.models import Organization, Membership
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.flush()
    db.add(Membership(user_id="u1", org_id=org.id, role="owner"))
    db.commit()
    db.refresh(org)
    return org


def _work_data():
    return {
        "brief": {"product_name": "烟台苹果", "platform": "抖音"},
        "strategy": {"goals": ["卖苹果"]},
        "creative": {"directions": [
            {"title": "果园现摘", "angle": "进果园",
             "selling_points": ["现摘现发"]}]},
        "storyboard": {"shots": [
            {"shot_no": 1, "voiceover": "现摘的苹果又脆又甜",
             "subtitle": "烟台苹果"}]},
        "shooting_list": {"items": [], "total_duration_sec": 60},
        "copy": {"platform": "抖音", "title": "苹果", "text": "现摘现发",
                 "hashtags": []},
    }


def _work(db, org_id, data=None):
    from backend.repositories.work_repository import WorkRepository
    w = WorkRepository(db).create(
        org_id=org_id, owner_id="u1", title="边界作品",
        data=data if data is not None else _work_data(), status="draft")
    db.commit()
    db.refresh(w)
    return w


def _conv(db, org_id, work_id):
    from backend.services.agent import create_conversation
    return create_conversation(db, org_id, "u1", work_id=work_id, brief={})


# ================= brief.py 边界 =================

class TestBriefBoundaries:
    def test_empty_text_raises(self):
        from backend.services.agent.brief import (
            BriefParseError, parse_brief_from_text)
        with pytest.raises(BriefParseError):
            parse_brief_from_text("", lambda p, f=None: {})

    def test_whitespace_text_raises(self):
        from backend.services.agent.brief import (
            BriefParseError, parse_brief_from_text)
        with pytest.raises(BriefParseError):
            parse_brief_from_text("   \n\t ", lambda p, f=None: {})

    def test_generate_exception_wrapped_not_leaked(self):
        """模型层异常 → BriefParseError；不直接向上传播原始异常。"""
        from backend.services.agent.brief import (
            BriefParseError, parse_brief_from_text)

        def boom(prompt, feedback=None):
            raise TimeoutError("upstream timeout 12s")

        with pytest.raises(BriefParseError, match="模型调用失败"):
            parse_brief_from_text("给苹果做视频", boom)

    def test_always_invalid_hits_repair_cap(self):
        """始终返回非法结构 → 抛错；调用次数 = 初次 + MAX_BRIEF_REPAIRS。"""
        from backend.services.agent.brief import (
            BriefParseError, MAX_BRIEF_REPAIRS, parse_brief_from_text)
        calls = []

        def bad(prompt, feedback=None):
            calls.append(prompt)
            return ["不是对象"]

        with pytest.raises(BriefParseError, match="修复上限"):
            parse_brief_from_text("给苹果做视频", bad)
        assert len(calls) == MAX_BRIEF_REPAIRS + 1 == 3

    def test_repair_once_then_ok(self):
        """首次非法、修复轮合法 → 成功且恰好 2 次调用。"""
        from backend.services.agent.brief import parse_brief_from_text
        calls = []

        def flaky(prompt, feedback=None):
            calls.append(feedback)
            if feedback is None:
                return {"product_name": "苹果", "budget_yuan": -5}
            return {"product_name": "苹果", "platform": "抖音"}

        b = parse_brief_from_text("给苹果做视频", flaky)
        assert b.product_name == "苹果"
        assert b.platform == "抖音"
        assert len(calls) == 2

    def test_budget_zero_means_unspecified(self):
        """budget_yuan=0 按设计视为"未指定"（_coerce_zero_to_none）→ None，
        不是透明默认（budget 不在 DEFAULTS 里），渲染为"（未指定）"。"""
        from backend.services.agent.brief import parse_brief_from_text
        b = parse_brief_from_text(
            "x", lambda p, f=None: {"product_name": "苹果",
                                   "budget_yuan": 0})
        assert b.budget_yuan is None

    def test_budget_over_max_rejected(self):
        """预算超 10_000_000 上限 → 校验失败走修复，修不好抛错。"""
        from backend.services.agent.brief import (
            BriefParseError, parse_brief_from_text)
        with pytest.raises(BriefParseError):
            parse_brief_from_text(
                "x", lambda p, f=None: {"product_name": "苹果",
                                       "budget_yuan": 10_000_001})

    def test_people_count_zero_means_unspecified(self):
        """people_count=0 视为"未指定"→ None，而非校验失败。"""
        from backend.services.agent.brief import parse_brief_from_text
        b = parse_brief_from_text(
            "x", lambda p, f=None: {"product_name": "苹果",
                                   "people_count": 0})
        assert b.people_count is None

    def test_unspecified_platform_gets_transparent_default(self):
        """未指定平台 → 透明默认"抖音"，并记入 confirmed_defaults。"""
        from backend.services.agent.brief import parse_brief_from_text
        b = parse_brief_from_text(
            "x", lambda p, f=None: {"product_name": "苹果"})
        assert b.platform == "抖音"
        assert any(m.startswith("平台：") for m in b.confirmed_defaults)

    def test_explicit_platform_not_marked(self):
        """显式指定的平台不进 confirmed_defaults。"""
        from backend.services.agent.brief import parse_brief_from_text
        b = parse_brief_from_text(
            "x", lambda p, f=None: {"product_name": "苹果",
                                   "platform": "快手"})
        assert b.platform == "快手"
        # "平台文案"含"平台"二字，用精确前缀匹配
        assert not any(m.startswith("平台：") for m in b.confirmed_defaults)

    def test_merge_brief_dict_none_base(self):
        from backend.services.agent.brief import merge_brief_dict
        out = merge_brief_dict(None, {"a": 1})
        assert out == {"a": 1}
        out = merge_brief_dict({"a": 1}, None)
        assert out == {"a": 1}

    def test_merge_brief_dict_nested_and_list(self):
        from backend.services.agent.brief import merge_brief_dict
        base = {"a": {"x": 1, "y": 2}, "tags": ["a", "b"]}
        delta = {"a": {"y": 3, "z": 4}, "tags": ["b", "c"], "k": ""}
        out = merge_brief_dict(base, delta)
        assert out["a"] == {"x": 1, "y": 3, "z": 4}
        assert out["tags"] == ["a", "b", "c"]
        assert out["k"] == ""  # 新 key 即使空值也补齐

    def test_merge_brief_dict_empty_delta_no_clobber(self):
        """delta 空值不覆盖 base 已有值；base 没有的新 key 即使空也补齐。"""
        from backend.services.agent.brief import merge_brief_dict
        out = merge_brief_dict({"a": 1, "b": "x"},
                               {"a": None, "b": "", "c": []})
        assert out == {"a": 1, "b": "x", "c": []}

    def test_merge_brief_update_wins(self):
        from backend.services.agent.brief import (
            Brief, merge_brief)
        base = Brief(product_name="苹果", platform="抖音")
        upd = Brief(product_name="梨", platform="")
        m = merge_brief(base, upd)
        assert m.product_name == "梨"
        assert m.platform == "抖音"  # 空值不覆盖

    def test_brief_to_prompt_text(self):
        from backend.services.agent.brief import (
            Brief, brief_to_prompt_text)
        t = brief_to_prompt_text(Brief(product_name="烟台苹果",
                                      platform="抖音"))
        assert "产品：烟台苹果" in t
        assert "平台/形式：抖音" in t
        t2 = brief_to_prompt_text(Brief())
        assert "（未指定）" in t2


# ================= modify.py 边界 =================

class TestModifyParseBoundaries:
    def test_empty_text_raises(self):
        from backend.services.agent.modify import (
            ModifyParseError, parse_modification_from_text)
        with pytest.raises(ModifyParseError):
            parse_modification_from_text("  ", lambda p, f=None: {})

    def test_429_passthrough_not_swallowed(self):
        """RateLimitedStop 必须穿透，不能被吞成 ModifyParseError。"""
        from backend.services.agent.modify import (
            ModifyParseError, parse_modification_from_text)
        from backend.services.agent.adapters import RateLimitedStop

        def limited(prompt, feedback=None):
            raise RateLimitedStop("撞 429")

        with pytest.raises(RateLimitedStop):
            parse_modification_from_text("改一下", limited)

    def test_other_exception_wrapped(self):
        from backend.services.agent.modify import (
            ModifyParseError, parse_modification_from_text)

        def boom(prompt, feedback=None):
            raise ConnectionError("reset")

        with pytest.raises(ModifyParseError, match="模型调用失败"):
            parse_modification_from_text("改一下", boom)

    def test_repair_cap(self):
        from backend.services.agent.modify import (
            MAX_MODIFY_REPAIRS, ModifyParseError,
            parse_modification_from_text)
        calls = []

        def bad(prompt, feedback=None):
            calls.append(1)
            return {"target_kind": "nope"}  # 非法枚举

        with pytest.raises(ModifyParseError, match="修复上限"):
            parse_modification_from_text("改一下", bad)
        assert len(calls) == MAX_MODIFY_REPAIRS + 1 == 3

    def test_unknown_operation_rejected(self):
        """白名单外 operation（如 exec_shell）直接被模型 schema 拒掉。"""
        from backend.services.agent.modify import (
            ModifyParseError, parse_modification_from_text)

        def hostile(prompt, feedback=None):
            return {"target_kind": "artifact", "target": "copy",
                    "operation": "exec_shell", "params": {},
                    "scope": "local", "touches_confirmed_fact": False}

        with pytest.raises(ModifyParseError):
            parse_modification_from_text("删库", hostile)


class TestModifyApplyBoundaries:
    def test_corrupted_data_json(self, db):
        """作品 data_json 损坏 → ModifyExecutionError，不写脏数据。"""
        org = _org(db)
        w = _work(db, org.id)
        w.data_json = "{坏掉的 json"
        db.commit()
        conv = _conv(db, org.id, w.id)
        from backend.services.agent import apply_modification
        from backend.services.agent.modify import ModifyExecutionError
        with pytest.raises(ModifyExecutionError, match="损坏"):
            apply_modification(db, "u1", org.id, w.id, conv.id,
                               "改标题", ai_generate=lambda p, f=None: {})

    def test_non_dict_data_json(self, db):
        w_org = _org(db)
        w = _work(db, w_org.id, data=[1, 2, 3])
        conv = _conv(db, w_org.id, w.id)
        from backend.services.agent import apply_modification
        from backend.services.agent.modify import ModifyExecutionError
        with pytest.raises(ModifyExecutionError, match="不是对象"):
            apply_modification(db, "u1", w_org.id, w.id, conv.id,
                               "改标题", ai_generate=lambda p, f=None: {})

    def test_restore_missing_version(self, db):
        org = _org(db)
        w = _work(db, org.id)
        from backend.services.agent import restore_modification
        from backend.repositories.base import EntityNotFound
        with pytest.raises(EntityNotFound):
            restore_modification(db, "u1", org.id, w.id, 999)

    def test_restore_non_member_rejected(self, db):
        org = _org(db)
        w = _work(db, org.id)
        from backend.services.agent import restore_modification
        from backend.repositories.base import EntityNotFound
        with pytest.raises(EntityNotFound):
            restore_modification(db, "stranger", org.id, w.id, 1)

    def test_snapshot_version_cap(self, db):
        """快照数超 MAX_VERSIONS_PER_WORK 时删最旧，保持上限。"""
        from backend.services.agent.modify import (
            MAX_VERSIONS_PER_WORK, _snapshot_work_version)
        from backend.models import WorkVersion
        org = _org(db)
        w = _work(db, org.id)
        for _ in range(MAX_VERSIONS_PER_WORK + 5):
            _snapshot_work_version(db, w, "u1")
        cnt = db.query(WorkVersion).filter_by(work_id=w.id).count()
        assert cnt == MAX_VERSIONS_PER_WORK
        nos = sorted(v.version_no for v in
                     db.query(WorkVersion).filter_by(work_id=w.id).all())
        assert nos[0] > 1  # 最旧的已被淘汰
        assert nos == sorted(nos)

    def test_protection_confirmed_fact(self):
        """触碰人工确认事实 → protection_reason 返回 confirmed_fact。"""
        from backend.services.agent.modify import (
            ModificationPlan, ModificationImpact, protection_reason)
        plan = ModificationPlan(target_kind="artifact", target="brief",
                                operation="rewrite", params={},
                                scope="local",
                                touches_confirmed_fact=True)
        impact = ModificationImpact(impacted_artifacts=["brief"],
                                    impacted_shots=[], checks_to_rerun=[],
                                    locked_shots_hit=[],
                                    touches_confirmed_fact=True,
                                    artifact_count=1)
        r = protection_reason(plan, impact)
        assert r is not None and r.startswith("confirmed_fact")

    def test_protection_wide_scope_threshold(self):
        """artifact 数恰为上限 → 放行；超上限 1 → 需确认（边界）。"""
        from backend.services.agent.modify import (
            MAX_AUTO_ARTIFACTS, ModificationPlan, ModificationImpact,
            protection_reason)
        plan = ModificationPlan(target_kind="artifact", target="brief",
                                operation="rewrite", params={},
                                scope="local",
                                touches_confirmed_fact=False)

        def impact_with(n):
            return ModificationImpact(
                impacted_artifacts=[f"a{i}" for i in range(n)],
                impacted_shots=[], checks_to_rerun=[],
                locked_shots_hit=[], touches_confirmed_fact=False,
                artifact_count=n)

        assert protection_reason(plan, impact_with(MAX_AUTO_ARTIFACTS)) is None
        r = protection_reason(plan, impact_with(MAX_AUTO_ARTIFACTS + 1))
        assert r is not None and r.startswith("wide_scope")

    def test_protection_no_issue_returns_none(self):
        from backend.services.agent.modify import (
            ModificationPlan, ModificationImpact, protection_reason)
        plan = ModificationPlan(target_kind="artifact", target="copy",
                                operation="rewrite", params={},
                                scope="local",
                                touches_confirmed_fact=False)
        impact = ModificationImpact(impacted_artifacts=["copy"],
                                    impacted_shots=[], checks_to_rerun=[],
                                    locked_shots_hit=[],
                                    touches_confirmed_fact=False,
                                    artifact_count=1)
        assert protection_reason(plan, impact) is None

    def test_analyze_impact_fact_field_detection(self, db):
        """params.updates 触碰确认事实字段 → touches_confirmed_fact。"""
        from backend.services.agent.modify import (
            ModificationPlan, analyze_impact, CONFIRMED_FACT_FIELDS)
        org = _org(db)
        w = _work(db, org.id)
        field = next(iter(CONFIRMED_FACT_FIELDS))
        plan = ModificationPlan(
            target_kind="artifact", target="brief", operation="rewrite",
            params={"updates": {field: "新值"}}, scope="local",
            touches_confirmed_fact=False)
        impact = analyze_impact(db, plan, _work_data(), w.id)
        assert impact.touches_confirmed_fact is True


# ================= orchestrator.py 边界 =================

class TestOrchestratorBoundaries:
    def _run(self, db, org_id, text, **kw):
        from backend.services.agent import run_one_shot_brief
        from backend.auth.database import get_session_factory
        sf = get_session_factory()
        return run_one_shot_brief(
            db, "u1", org_id, text,
            session_factory=lambda: sf(), **kw)

    def test_non_member_rejected(self, db):
        """非组织成员调一句话生成 → EntityNotFound（404 防枚举）。"""
        from backend.repositories.base import EntityNotFound
        from backend.services.agent import run_one_shot_brief
        from backend.auth.database import get_session_factory
        org = _org(db)
        sf = get_session_factory()
        with pytest.raises(EntityNotFound):
            run_one_shot_brief(
                db, "stranger", org.id, "给苹果做视频",
                session_factory=lambda: sf(),
                ai_generate=lambda p, f=None: {})

    def test_empty_brief_failed_not_crash(self, db):
        """空 brief → run 落 failed，错误信息可读，不抛未捕获异常。"""
        org = _org(db)
        out = self._run(db, org.id, "   ",
                        ai_generate=lambda p, f=None: {})
        assert out["run_status"] == "failed"
        assert out["summary"].get("error") or out["summary"].get("errors")

    def test_cancel_before_first_tool(self, db):
        """启动即取消 → cancelled，task 状态同步。"""
        org = _org(db)
        out = self._run(db, org.id, "给苹果做视频",
                        ai_generate=lambda p, f=None: {
                            "product_name": "苹果"},
                        is_cancelled=lambda: True)
        assert out["run_status"] == "cancelled"
        from backend.ai_tasks.repository import AITaskRepository
        task = AITaskRepository(db).get(out["task_id"])
        assert task.status == "cancelled"
        assert task.payload_json["run_status"] == "cancelled"

    def test_model_caller_step_cap(self, db):
        """_ModelCaller 模型步超 MAX_MODEL_STEPS_PER_RUN → ModelStepsExceeded。"""
        from backend.services.agent.orchestrator import _ModelCaller
        from backend.services.agent.planner import MAX_MODEL_STEPS_PER_RUN
        from backend.services.agent.adapters import ModelStepsExceeded
        c = _ModelCaller("t1", "o1", fake=lambda p, f=None: {"ok": True})
        for _ in range(MAX_MODEL_STEPS_PER_RUN):
            c.generate("p")
        with pytest.raises(ModelStepsExceeded):
            c.generate("p")
        # 上限命中发生在 try 之前，finally 不再累加；不再发起调用
        assert c.calls == MAX_MODEL_STEPS_PER_RUN

    def test_model_caller_429_latch(self, db):
        """fake 抛 429 特征异常 → 置 rate_limited，后续调用直接硬停。"""
        from backend.services.agent.orchestrator import _ModelCaller
        from backend.services.agent.adapters import RateLimitedStop
        calls = []

        def fake429(prompt, feedback=None):
            calls.append(1)
            e = Exception("429 Too Many Requests")
            raise e

        c = _ModelCaller("t1", "o1", fake=fake429)
        with pytest.raises(RateLimitedStop):
            c.generate("p")
        assert c.rate_limited is True
        with pytest.raises(RateLimitedStop):
            c.generate("p")  # 闩锁：不再调用 fake
        assert len(calls) == 1

    def test_brief_text_truncated_to_2000(self, db):
        """超长 brief 原文存 brief_json 时截断到 2000 字符。"""
        org = _org(db)
        long_text = "苹果" * 2000
        out = self._run(db, org.id, long_text,
                        ai_generate=lambda p, f=None: {
                            "product_name": "苹果"},
                        is_cancelled=lambda: True)
        from backend.ai_tasks.repository import AITaskRepository
        task = AITaskRepository(db).get(out["task_id"])
        stored = (task.payload_json.get("brief") or {}).get("text", "")
        assert 0 < len(stored) <= 2000
        assert stored == long_text[:2000]


# ================= quality_gates.py 边界 =================

def _valid_artifacts():
    return {
        "strategy": {
            "goals": ["7 天卖出 500 单"],
            "target_audience": "注重健康的年轻人",
            "content_pillars": ["果园实拍"],
            "key_messages": ["现摘现发"]},
        "creative": {
            "directions": [
                {"title": "果园现摘", "angle": "跟着果农进果园",
                 "selling_points": ["现摘现发", "脆甜多汁"]}],
            "recommended_index": 0},
        "storyboard": {
            "shots": [
                {"shot_no": 1, "scene": "果园", "shot_type": "中景",
                 "duration_sec": 60, "voiceover": "现摘的苹果又脆又甜",
                 "subtitle": "烟台苹果"}]},
        "shooting_list": {
            "items": [{"shot_no": 1, "scene": "果园", "duration_sec": 60}],
            "total_duration_sec": 60, "crew_size": 2},
        "copy": {"platform": "抖音", "title": "烟台苹果现摘现发",
                 "text": "果园现摘的苹果，又脆又甜，欢迎下单",
                 "hashtags": ["烟台苹果"]},
    }


class TestQualityGateBoundaries:
    def test_validate_unknown_kind(self):
        from backend.services.agent.quality_gates import validate_artifact
        obj, issues = validate_artifact("nope", {})
        assert obj is None
        assert any("未知" in i.message for i in issues)

    def test_validate_non_dict(self):
        from backend.services.agent.quality_gates import validate_artifact
        obj, issues = validate_artifact("strategy", ["x"])
        assert obj is None
        assert issues

    def test_validate_extra_keys_ignored(self):
        """_Strict 用 extra="ignore"：多余字段被忽略，不导致失败。"""
        from backend.services.agent.quality_gates import validate_artifact
        data = {"goals": ["卖"], "target_audience": "年轻人",
                "content_pillars": ["实拍"], "key_messages": ["现摘"],
                "hacker_field": "x"}
        obj, issues = validate_artifact("strategy", data)
        assert obj is not None and not issues
        assert not hasattr(obj, "hacker_field")

    def test_validate_minimal_ok(self):
        from backend.services.agent.quality_gates import validate_artifact
        data = {"goals": ["卖"], "target_audience": "年轻人",
                "content_pillars": ["实拍"], "key_messages": ["现摘"]}
        obj, issues = validate_artifact("strategy", data)
        assert obj is not None and not issues

    def test_fact_grounding_high_risk_unmarked(self):
        """高风险宣称无引用、未标待核实 → issue。"""
        from backend.services.agent.quality_gates import (
            check_fact_grounding)
        arts = _valid_artifacts()
        arts["copy"]["text"] = "吃了降血压的神奇苹果，快来买"
        brief = {"items_to_verify": []}
        issues = check_fact_grounding(brief, arts, facts=[])
        assert any("降压" in i.message or "降血压" in i.message
                   for i in issues)

    def test_fact_grounding_marked_ok(self):
        """同一宣称标了待核实 → 合规，无 issue。"""
        from backend.services.agent.quality_gates import (
            check_fact_grounding)
        arts = _valid_artifacts()
        arts["copy"]["text"] = ("吃了降血压的神奇苹果"
                                "（待核实，不可作为确定宣传），快来买")
        brief = {"items_to_verify": []}
        issues = check_fact_grounding(brief, arts, facts=[])
        assert not issues

    def test_fact_grounding_with_usable_fact(self):
        """句子含事实原文片段 → 视为有引用，合规。"""
        from backend.services.agent.quality_gates import (
            check_fact_grounding)
        arts = _valid_artifacts()
        arts["copy"]["text"] = "本批苹果亩产 8000 斤，欢迎订购"
        brief = {"items_to_verify": []}
        facts = [{"id": "f1", "content": "本批苹果亩产 8000 斤，现摘现发"}]
        issues = check_fact_grounding(brief, arts, facts=facts)
        assert not [i for i in issues if "亩产" in i.message]

    def test_fact_grounding_brief_verify_items_cover(self):
        """brief.items_to_verify 覆盖该关键词 → 合规。"""
        from backend.services.agent.quality_gates import (
            check_fact_grounding)
        arts = _valid_artifacts()
        arts["copy"]["text"] = "吃了降血压的神奇苹果"
        brief = {"items_to_verify": ["降血压功效未核实"]}
        issues = check_fact_grounding(brief, arts, facts=[])
        assert not issues

    def test_fact_grounding_blood_sugar_keyword(self):
        """回归："降血糖"同样被高风险关键词兜底（"血压/血糖"）。"""
        from backend.services.agent.quality_gates import (
            check_fact_grounding)
        arts = _valid_artifacts()
        arts["copy"]["text"] = "吃了降血糖的神奇苹果"
        issues = check_fact_grounding({"items_to_verify": []}, arts,
                                      facts=[])
        assert any("血糖" in i.message for i in issues)

    def test_verify_persistence_missing_work(self, db):
        from backend.services.agent.quality_gates import verify_persistence
        org = _org(db)
        issues = verify_persistence(db, "no-such-work", org.id,
                                    ["strategy"])
        assert issues and "不存在" in issues[0].message

    def test_verify_persistence_wrong_org(self, db):
        """跨组织重读 → 视为不存在（防越权读）。"""
        from backend.services.agent.quality_gates import verify_persistence
        org = _org(db)
        w = _work(db, org.id)
        issues = verify_persistence(db, w.id, "other-org", ["strategy"])
        assert issues

    def test_verify_persistence_corrupted_json(self, db):
        org = _org(db)
        w = _work(db, org.id)
        w.data_json = "[[["
        db.commit()
        from backend.services.agent.quality_gates import verify_persistence
        issues = verify_persistence(db, w.id, org.id, ["strategy"])
        assert issues and "损坏" in issues[0].message

    def test_run_gates_all_pass_completed(self):
        from backend.services.agent.quality_gates import run_quality_gates
        brief = {"product_name": "烟台苹果", "items_to_verify": []}
        rep = run_quality_gates(brief, _valid_artifacts(), facts=[])
        assert rep.passed is True
        assert rep.verdict == "completed"

    def test_run_gates_missing_required_failed(self):
        from backend.services.agent.quality_gates import run_quality_gates
        brief = {"product_name": "烟台苹果", "items_to_verify": []}
        arts = _valid_artifacts()
        del arts["copy"]
        rep = run_quality_gates(brief, arts, facts=[])
        assert rep.passed is False
        assert rep.verdict == "failed"
        failed = [g for g in rep.gates
                  if g.required and not g.passed]
        assert any(g.name == "schema:copy" for g in failed)
