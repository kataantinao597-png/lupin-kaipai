"""Phase 6 测试：连续对话与局部修改（意图解析 / 影响分析 / 保护规则 /
版本快照与一键恢复 / patch 语义执行 / 改后重检 / 修改摘要 / 跨作品拒绝）。

- 业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。
- 模型调用一律经 ai_generate fake 注入（(prompt, feedback)->dict），
  不烧真实 token；1 次真实意图解析冒烟单独走脚本（见 EXECUTION_LOG）。
- 回归：test_agent_orchestrator_phase5.py + test_agent_api_phase4a.py 全绿。

运行：./venv/bin/python -m pytest tests/test_agent_modify_phase6.py
"""
import copy
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
    url = f"sqlite:///{tmp_path}/phase6.db"
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


def _shots(n=5, stabilizer_nos=(2, 4), risky=False):
    out = []
    for i in range(1, n + 1):
        notes = "稳定器环绕拍摄" if i in stabilizer_nos else "三脚架固定机位"
        vo = (f"第{i}镜：果园里现摘的苹果，又脆又甜"
              if not risky else f"第{i}镜：包治百病、吃了降血压的神奇苹果")
        out.append({
            "shot_no": i, "scene": f"果园场景{i}", "shot_type": "中景",
            "duration_sec": 12, "voiceover": vo,
            "subtitle": f"字幕{i}", "shoot_notes": notes})
    return out


def _work_data(**kw):
    d = {
        "brief": {
            "product_name": "烟台苹果", "platform": "抖音",
            "marketing_goal": "带货卖苹果",
            "budget_yuan": 2000, "people_count": 2,
            "devices": ["手机", "三脚架"],
            "video_duration_sec": 60, "style": "拉呱风",
            "items_to_verify": [],
        },
        "strategy": {"goals": ["7 天卖出 500 单"]},
        "creative": {"directions": [
            {"title": "果园现摘", "angle": "跟着果农进果园现摘",
             "selling_points": ["现摘现发", "脆甜多汁"]}]},
        "storyboard": {"shots": _shots()},
        "shooting_list": {"items": [], "total_duration_sec": 60},
        "copy": {"platform": "抖音", "title": "烟台苹果，现摘现发",
                 "text": "果园现摘的苹果，又脆又甜，欢迎下单",
                 "hashtags": ["烟台苹果", "助农"]},
    }
    d.update(kw)
    return d


def _work(db, org_id, data=None, title="苹果带货视频"):
    from backend.repositories.work_repository import WorkRepository
    w = WorkRepository(db).create(
        org_id=org_id, owner_id="u1", title=title,
        data=data if data is not None else _work_data(), status="draft")
    db.commit()
    db.refresh(w)
    return w


def _conv(db, org_id, work_id):
    from backend.services.agent import create_conversation
    return create_conversation(db, org_id, "u1", work_id=work_id,
                               brief={"work_title": "苹果带货视频"})


def _work_json(db, org_id, work_id):
    from backend.repositories.work_repository import WorkRepository
    w = WorkRepository(db).get(work_id, org_id)
    return json.loads(w.data_json), w.version


class FakeBrain:
    """fake 模型大脑：按 prompt 标记分发意图解析 / 内容生成。

    意图解析 prompt 含场景标记「SCN:<key>」时返回对应 plan；
    内容生成 prompt 按 kind 标记返回。
    """

    def __init__(self, plans=None, contents=None):
        self.plans = plans or {}
        self.contents = contents or {}
        self.calls = []

    def __call__(self, prompt, feedback=None):
        self.calls.append(prompt)
        if "【修改意图解析】" in prompt:
            for key, plan in self.plans.items():
                if f"SCN:{key}" in prompt:
                    return copy.deepcopy(plan)
            assert self.plans, "fake brain 没有可用的 plan"
            return copy.deepcopy(next(iter(self.plans.values())))
        if "【风险解释】" in prompt:
            return copy.deepcopy(self.contents.get(
                "explain",
                {"explanation": "功效宣称无事实支撑，属高风险",
                 "safer_text": "果园现摘的苹果，又脆又甜（功效待核实）"}))
        if "【局部重写" in prompt:
            kind = ("shot" if "镜头" in prompt else "artifact")
            return copy.deepcopy(self.contents.get(kind, {"updates": {}}))
        raise AssertionError(f"未预期的模型调用: {prompt[:80]}")


def _plan(**kw):
    p = {"target_kind": "artifact", "target": "brief",
         "operation": "rewrite", "params": {}, "scope": "local",
         "touches_confirmed_fact": False}
    p.update(kw)
    return p


def _apply(db, org_id, work_id, conv_id, text, brain, **kw):
    from backend.services.agent import apply_modification
    return apply_modification(
        db, "u1", org_id, work_id, conv_id, text,
        ai_generate=brain, **kw)

class TestTypicalRequests:
    """任务书 §十的 6 个典型请求，各一个固定用例。"""

    def test_1_style_rewrite(self, db):
        """典型①：整体风格改得更有山东本土特色。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, v0 = _work_json(db, org.id, w.id)
        brain = FakeBrain(
            plans={"style": _plan(
                target_kind="artifact", target="brief",
                operation="rewrite",
                params={"instruction": "风格改成更有山东本土特色",
                        "fields": ["style"]})},
            contents={"artifact": {"updates": {
                "style": "山东本土特色拉呱风，带着煎饼卷大葱的实在劲儿"}}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:style 整体风格改得更有山东本土特色。", brain)
        assert out["status"] == "completed", out
        after, v1 = _work_json(db, org.id, w.id)
        assert v1 == v0 + 1
        # 目标字段已改
        assert after["brief"]["style"] == \
            "山东本土特色拉呱风，带着煎饼卷大葱的实在劲儿"
        # 无关 artifact 零变化
        for k in ("storyboard", "copy", "strategy", "creative",
                  "shooting_list"):
            assert after[k] == before[k], k
        # brief 内其他字段零变化
        for k, val in before["brief"].items():
            if k != "style":
                assert after["brief"][k] == val, k
        # 重检已触发并写入 step
        assert set(out["checks_rerun"]) == {"feasibility", "delivery"}
        assert out["rechecks_passed"] is True
        # 摘要结构
        assert out["changed"]["operation"] == "rewrite"
        assert out["snapshot_version_no"] == 1
        assert out["restorable"] is True
        from backend.services.agent import AgentStep
        tool_ids = {r.tool_id for r in
                    db.query(AgentStep).filter_by(
                        task_id=out["task_id"]).all()}
        assert {"modify_parse", "modify_impact", "modify_generate",
                "modify", "modify_recheck"} <= tool_ids

    def test_2_budget_people_replace(self, db):
        """典型②：预算改为 300 元，拍摄人员只有 1 人。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, _ = _work_json(db, org.id, w.id)
        brain = FakeBrain(plans={"budget": _plan(
            target_kind="field", target="brief", operation="replace",
            params={"updates": {"brief.budget_yuan": 300,
                                "brief.people_count": 1}})})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:budget 预算改为 300 元，拍摄人员只有 1 人。", brain)
        assert out["status"] == "completed", out
        after, _ = _work_json(db, org.id, w.id)
        assert after["brief"]["budget_yuan"] == 300
        assert after["brief"]["people_count"] == 1
        # 无关 artifact 零变化（无模型参与的纯字段替换）
        for k in ("storyboard", "copy", "strategy", "creative",
                  "shooting_list"):
            assert after[k] == before[k], k
        # 可行性重检记录了低预算提示（诚实的不通过，而非放水通过）
        feas = next(x for x in out["rechecks"]
                    if x["check"] == "feasibility")
        assert feas["passed"] is False
        assert any("300" in i for i in feas["issues"])
        assert out["changed"]["fields"] == [
            "brief.budget_yuan", "brief.people_count"]

    def test_3_device_shot_rewrite(self, db):
        """典型③：我没有稳定器，把相关镜头换掉。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, _ = _work_json(db, org.id, w.id)
        new_shots = [
            {"shot_no": 2, "scene": "果园场景2", "shot_type": "中景",
             "duration_sec": 12,
             "voiceover": "第2镜：果园里现摘的苹果，又脆又甜",
             "subtitle": "字幕2", "shoot_notes": "三脚架固定机位拍摄"},
            {"shot_no": 4, "scene": "果园场景4", "shot_type": "中景",
             "duration_sec": 12,
             "voiceover": "第4镜：果园里现摘的苹果，又脆又甜",
             "subtitle": "字幕4", "shoot_notes": "手持手机平稳拍摄"},
        ]
        brain = FakeBrain(
            plans={"device": _plan(
                target_kind="shot", target="shot", operation="rewrite",
                params={"shot_nos": [2, 4],
                        "instruction": "去掉稳定器相关拍摄，换三脚架/手持"})},
            contents={"shot": {"shots": new_shots}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:device 我没有稳定器，把相关镜头换掉。", brain)
        assert out["status"] == "completed", out
        after, _ = _work_json(db, org.id, w.id)
        shots = {s["shot_no"]: s
                 for s in after["storyboard"]["shots"]}
        # 目标镜头已换
        assert "稳定器" not in shots[2]["shoot_notes"]
        assert "稳定器" not in shots[4]["shoot_notes"]
        # 其他镜头零变化
        for no in (1, 3, 5):
            old = next(s for s in before["storyboard"]["shots"]
                       if s["shot_no"] == no)
            assert shots[no] == old, no
        assert out["changed"]["shots"] == [2, 4]
        # storyboard 相关检查全跑了
        assert {"duration", "feasibility", "fact_grounding", "risk",
                "delivery"} <= set(out["checks_rerun"])

    def test_4_voiceover_compress_30s(self, db):
        """典型④：口播压缩到 30 秒。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        short = []
        for i in range(1, 6):
            short.append({
                "shot_no": i, "scene": f"果园场景{i}", "shot_type": "中景",
                "duration_sec": 6, "voiceover": f"第{i}镜：现摘苹果，又脆又甜",
                "subtitle": f"字幕{i}", "shoot_notes": "三脚架固定机位"})
        brain = FakeBrain(
            plans={"compress": _plan(
                target_kind="artifact", target="storyboard",
                operation="rewrite",
                params={"instruction": "口播总时长压缩到30秒",
                        "fields": ["shots"],
                        "field_updates": {
                            "brief.video_duration_sec": 30}})},
            contents={"artifact": {"updates": {"shots": short}}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:compress 口播压缩到 30 秒。", brain)
        assert out["status"] == "completed", out
        after, _ = _work_json(db, org.id, w.id)
        total = sum(s["duration_sec"]
                    for s in after["storyboard"]["shots"])
        assert total == 30
        assert after["brief"]["video_duration_sec"] == 30
        dur = next(x for x in out["rechecks"]
                   if x["check"] == "duration")
        assert dur["passed"] is True
        assert dur["total_sec"] == 30
        assert set(out["impacted_artifacts"]) == {"storyboard", "brief"}

    def test_5_single_shot_only(self, db):
        """典型⑤：只重做第 3 个镜头，其他不变。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, _ = _work_json(db, org.id, w.id)
        new3 = {"shot_no": 3, "scene": "果园特写苹果", "shot_type": "特写",
                "duration_sec": 10, "voiceover": "第3镜：看这苹果，多水灵",
                "subtitle": "字幕3-新", "shoot_notes": "三脚架固定机位"}
        brain = FakeBrain(
            plans={"shot3": _plan(
                target_kind="shot", target="shot:3", operation="rewrite",
                params={"instruction": "重做第3个镜头"})},
            contents={"shot": {"shots": [new3]}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:shot3 只重做第 3 个镜头，其他不变。", brain)
        assert out["status"] == "completed", out
        after, _ = _work_json(db, org.id, w.id)
        shots = {s["shot_no"]: s
                 for s in after["storyboard"]["shots"]}
        assert shots[3]["scene"] == "果园特写苹果"
        for no in (1, 2, 4, 5):
            old = next(s for s in before["storyboard"]["shots"]
                       if s["shot_no"] == no)
            assert shots[no] == old, f"第{no}镜不应变化"
        # 其他 artifact 零变化
        for k in ("brief", "copy", "strategy", "creative"):
            assert after[k] == before[k], k

    def test_6_explain_risk_safer_copy(self, db):
        """典型⑥：解释风险，并根据已确认事实给出更稳妥的文案。"""
        org = _org(db)
        _member(db, org.id)
        data = _work_data()
        data["copy"]["text"] = "包治百病、吃了降血压的神奇苹果，赶紧下单"
        w = _work(db, org.id, data=data)
        conv = _conv(db, org.id, w.id)
        brain = FakeBrain(
            plans={"explain": _plan(
                target_kind="artifact", target="copy",
                operation="explain",
                params={"text": "包治百病、吃了降血压",
                        "context": "功效宣称无事实支撑"})},
            contents={"explain": {
                "explanation": "“包治百病/降血压”属功效宣称，无事实支撑，"
                               "违反广告法高风险表述",
                "safer_text": "果园现摘的苹果，又脆又甜（功效待核实）"}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:explain 解释这个风险，并根据已确认事实给出更稳妥的文案。",
                     brain)
        assert out["status"] == "completed", out
        after, _ = _work_json(db, org.id, w.id)
        assert after["copy"]["text"] == "果园现摘的苹果，又脆又甜（功效待核实）"
        # 风险解释后事实引用重检通过
        fg = next(x for x in out["rechecks"]
                  if x["check"] == "fact_grounding")
        assert fg["passed"] is True, fg
        risk = next(x for x in out["rechecks"] if x["check"] == "risk")
        assert risk["high_risk_count"] == 0

class TestProtection:
    """保护规则：锁定镜头 / 人工确认事实 / 大范围覆盖 / 高风险变化。"""

    def _lock_shot(self, db, work_id, shot_no=3):
        from backend.repositories.storyboard_repository import (
            StoryboardRepository)
        repo = StoryboardRepository(db)
        sb = repo.create(work_id, title="主分镜")
        shot = repo.add_shot(sb.id, shot_no, content={"scene": "x"})
        repo.set_shot_lock(sb.id, shot.id, True)
        db.commit()

    def test_locked_shot_waiting_user_then_confirm(self, db):
        """锁定镜头：先 waiting_user（作品零变化），确认后才执行。"""
        from backend.services.agent import confirm_modification
        from backend.models import WorkVersion
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        self._lock_shot(db, w.id, shot_no=3)
        before, v0 = _work_json(db, org.id, w.id)
        new3 = {"shot_no": 3, "scene": "新场景3", "shot_type": "特写",
                "duration_sec": 10, "voiceover": "新口播3",
                "subtitle": "新字幕3", "shoot_notes": "三脚架"}
        brain = FakeBrain(
            plans={"shot3": _plan(
                target_kind="shot", target="shot:3", operation="rewrite",
                params={"instruction": "重做第3个镜头"})},
            contents={"shot": {"shots": [new3]}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:shot3 只重做第 3 个镜头，其他不变。", brain)
        assert out["status"] == "waiting_user", out
        assert out["confirm_reason"].startswith("shot_locked")
        # 挂起期间作品零变化、无快照
        mid, v1 = _work_json(db, org.id, w.id)
        assert mid == before and v1 == v0
        assert db.query(WorkVersion).filter_by(work_id=w.id).count() == 0
        # 用户确认 → 执行
        done = confirm_modification(db, out["task_id"], 0, True,
                                    org_id=org.id)
        assert done["status"] == "completed", done
        after, v2 = _work_json(db, org.id, w.id)
        assert v2 == v0 + 1
        shots = {s["shot_no"]: s
                 for s in after["storyboard"]["shots"]}
        assert shots[3]["scene"] == "新场景3"
        assert done["snapshot_version_no"] == 1
        assert done["restorable"] is True

    def test_locked_shot_reject_cancels(self, db):
        """锁定镜头：用户拒绝 → cancelled，作品不变。"""
        from backend.services.agent import confirm_modification
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        self._lock_shot(db, w.id, shot_no=3)
        before, v0 = _work_json(db, org.id, w.id)
        brain = FakeBrain(
            plans={"shot3": _plan(
                target_kind="shot", target="shot:3", operation="rewrite",
                params={"instruction": "重做第3个镜头"})},
            contents={"shot": {"shots": [
                {"shot_no": 3, "scene": "新场景3", "shot_type": "特写",
                 "duration_sec": 10, "voiceover": "新口播3",
                 "subtitle": "新字幕3", "shoot_notes": "三脚架"}]}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:shot3 只重做第 3 个镜头，其他不变。", brain)
        assert out["status"] == "waiting_user"
        done = confirm_modification(db, out["task_id"], 0, False,
                                    org_id=org.id)
        assert done["status"] == "cancelled"
        mid, v1 = _work_json(db, org.id, w.id)
        assert mid == before and v1 == v0

    def test_confirmed_fact_needs_confirmation(self, db):
        """人工确认事实的修改（产品名）：先确认。"""
        from backend.services.agent import confirm_modification
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        brain = FakeBrain(plans={"rename": _plan(
            target_kind="field", target="brief.product_name",
            operation="replace", params={"value": "烟台红富士"},
            touches_confirmed_fact=True)})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:rename 把产品名改成烟台红富士。", brain)
        assert out["status"] == "waiting_user", out
        assert out["confirm_reason"].startswith("confirmed_fact")
        before, _ = _work_json(db, org.id, w.id)
        assert before["brief"]["product_name"] == "烟台苹果"  # 未执行
        done = confirm_modification(db, out["task_id"], 0, True,
                                    org_id=org.id)
        assert done["status"] == "completed"
        after, _ = _work_json(db, org.id, w.id)
        assert after["brief"]["product_name"] == "烟台红富士"

    def test_wide_scope_needs_confirmation(self, db):
        """大范围覆盖（影响 4 个 artifact）：先确认。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        brain = FakeBrain(
            plans={"wide": _plan(
                target_kind="artifact", target="storyboard",
                operation="rewrite",
                params={"instruction": "整体推倒重来",
                        "fields": ["shots"],
                        "field_updates": {
                            "copy.text": "新文案",
                            "creative.directions": [],
                            "strategy.goals": ["新目标"]}})},
            contents={"artifact": {"updates": {"shots": _shots()}}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:wide 全部推倒重来。", brain)
        assert out["status"] == "waiting_user", out
        assert out["confirm_reason"].startswith("wide_scope")
        assert set(out["impact"]["impacted_artifacts"]) == {
            "storyboard", "copy", "creative", "strategy"}

    def test_high_risk_change_needs_confirmation(self, db):
        """高风险变化：生成内容含高风险宣称 → 先确认，作品不变。"""
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, v0 = _work_json(db, org.id, w.id)
        risky3 = {"shot_no": 3, "scene": "果园场景3", "shot_type": "中景",
                  "duration_sec": 12,
                  "voiceover": "第3镜：包治百病，吃了降血压的神奇苹果",
                  "subtitle": "字幕3", "shoot_notes": "三脚架"}
        brain = FakeBrain(
            plans={"shot3": _plan(
                target_kind="shot", target="shot:3", operation="rewrite",
                params={"instruction": "重做第3个镜头"})},
            contents={"shot": {"shots": [risky3]}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:shot3 只重做第 3 个镜头，其他不变。", brain)
        assert out["status"] == "waiting_user", out
        assert out["confirm_reason"].startswith("high_risk")
        mid, v1 = _work_json(db, org.id, w.id)
        assert mid == before and v1 == v0


class TestVersionAndSafety:
    """版本恢复 / 跨作品拒绝。"""

    def test_version_restore(self, db):
        """一键恢复：回到修改前快照。"""
        from backend.services.agent import restore_modification
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        conv = _conv(db, org.id, w.id)
        before, _ = _work_json(db, org.id, w.id)
        brain = FakeBrain(
            plans={"style": _plan(
                target_kind="artifact", target="brief",
                operation="rewrite",
                params={"instruction": "改风格", "fields": ["style"]})},
            contents={"artifact": {"updates": {"style": "新风格"}}})
        out = _apply(db, org.id, w.id, conv.id,
                     "SCN:style 整体风格改得更有山东本土特色。", brain)
        assert out["status"] == "completed"
        snap_no = out["snapshot_version_no"]
        changed, _ = _work_json(db, org.id, w.id)
        assert changed["brief"]["style"] == "新风格"
        # 恢复
        r = restore_modification(db, "u1", org.id, w.id, snap_no)
        assert r["restored"] is True
        restored, _ = _work_json(db, org.id, w.id)
        assert restored == before
        # 恢复操作本身可撤销：快照数应为 2（修改前 + 恢复前）
        from backend.models import WorkVersion
        assert db.query(WorkVersion).filter_by(work_id=w.id).count() == 2

    def test_restore_nonexistent_version_404(self, db):
        """恢复不存在的版本 → 404。"""
        from backend.services.agent import restore_modification
        from backend.repositories.base import EntityNotFound
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        with pytest.raises(EntityNotFound):
            restore_modification(db, "u1", org.id, w.id, 999)

    def test_cross_work_rejected(self, db):
        """跨作品拒绝：会话绑定作品 A，请求改作品 B → 404，不执行。"""
        from backend.services.agent import apply_modification
        from backend.repositories.base import EntityNotFound
        org = _org(db)
        _member(db, org.id)
        wa = _work(db, org.id, title="作品A")
        wb = _work(db, org.id, title="作品B")
        conv = _conv(db, org.id, wa.id)  # 会话绑定 A
        brain = FakeBrain(plans={"x": _plan()})
        with pytest.raises(EntityNotFound):
            apply_modification(db, "u1", org.id, wb.id, conv.id,
                               "SCN:x 改一下。", ai_generate=brain)
        # B 完全没被碰过
        _, vb = _work_json(db, org.id, wb.id)
        assert vb == 1

    def test_cross_org_conversation_rejected(self, db):
        """跨组织会话 → 404。"""
        from backend.services.agent import apply_modification
        from backend.repositories.base import EntityNotFound
        org_a = _org(db, name="A社")
        org_b = _org(db, name="B社")
        _member(db, org_a.id)
        _member(db, org_b.id)
        wa = _work(db, org_a.id)
        conv_b = _conv(db, org_b.id, wa.id)  # 会话在 B 组织
        brain = FakeBrain(plans={"x": _plan()})
        with pytest.raises(EntityNotFound):
            apply_modification(db, "u1", org_a.id, wa.id, conv_b.id,
                               "SCN:x 改一下。", ai_generate=brain)


class TestParseRepair:
    """意图解析：受控修复 / 上限 / 429 穿透。"""

    def test_parse_repair_then_success(self):
        """非法 JSON → 受控修复后成功。"""
        from backend.services.agent import parse_modification_from_text
        calls = {"n": 0}

        def fake(prompt, feedback=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"oops": "not a plan"}  # 缺必需字段
            return _plan(target_kind="field", target="brief.budget_yuan",
                         operation="replace",
                         params={"value": 300})

        plan = parse_modification_from_text("预算改成300", fake)
        assert plan.target == "brief.budget_yuan"
        assert plan.operation == "replace"
        assert calls["n"] == 3  # 初次 + 2 次修复

    def test_parse_exhausted_raises(self):
        """超修复上限 → ModifyParseError。"""
        from backend.services.agent import (
            parse_modification_from_text, ModifyParseError)

        def fake(prompt, feedback=None):
            return "not a dict"

        with pytest.raises(ModifyParseError):
            parse_modification_from_text("预算改成300", fake)

    def test_empty_input_raises(self):
        from backend.services.agent import (
            parse_modification_from_text, ModifyParseError)
        with pytest.raises(ModifyParseError):
            parse_modification_from_text("  ", lambda p, f=None: {})

    def test_429_hard_stop_propagates(self):
        """429 硬停穿透，不被吞成解析失败。"""
        from backend.services.agent import (
            parse_modification_from_text, RateLimitedStop)

        def fake(prompt, feedback=None):
            raise RateLimitedStop("429 too many requests")

        with pytest.raises(RateLimitedStop):
            parse_modification_from_text("预算改成300", fake)


class TestImpactAnalysis:
    """影响分析单元测试。"""

    def test_impact_fields(self, db):
        from backend.services.agent import analyze_impact, ModificationPlan
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        data, _ = _work_json(db, org.id, w.id)
        plan = ModificationPlan(**_plan(
            target_kind="field", target="brief.budget_yuan",
            operation="replace", params={"value": 300}))
        imp = analyze_impact(db, plan, data, w.id)
        assert imp.impacted_artifacts == ["brief"]
        assert imp.checks_to_rerun == ["feasibility", "delivery"]
        assert imp.locked_shots_hit == []
        assert imp.artifact_count == 1

    def test_impact_shot_with_lock(self, db):
        from backend.services.agent import (
            analyze_impact, protection_reason)
        org = _org(db)
        _member(db, org.id)
        w = _work(db, org.id)
        data, _ = _work_json(db, org.id, w.id)
        from backend.repositories.storyboard_repository import (
            StoryboardRepository)
        repo = StoryboardRepository(db)
        sb = repo.create(w.id, title="主分镜")
        shot = repo.add_shot(sb.id, 2, content={"scene": "x"})
        repo.set_shot_lock(sb.id, shot.id, True)
        db.commit()
        from backend.services.agent import ModificationPlan
        plan = ModificationPlan(**_plan(
            target_kind="shot", target="shot:2",
            operation="rewrite", params={"instruction": "x"}))
        imp = analyze_impact(db, plan, data, w.id)
        assert imp.impacted_shots == [2]
        assert imp.locked_shots_hit == [2]
        assert imp.impacted_artifacts == ["storyboard"]
        reason = protection_reason(plan, imp)
        assert reason and reason.startswith("shot_locked")
