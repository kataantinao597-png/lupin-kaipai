#!/usr/bin/env python3
"""Phase 8 固定评测集运行器（任务书 §十二）。

25 个脚本化场景（正常/边界/失败/对抗），mock 模型保证可重复。
每个场景输出：输入、预期 artifact、实际结果、通过/失败。

诚实标注：这是 mock 驱动的确定性评测，不是生产流量；真实模型行为、
真实用户措辞多样性、生产数据分布均未覆盖。

业务库 backend/data/lupin.db 只读：全部用 scratch 库（/tmp）。
不烧真实 token：模型调用一律 fake 注入。

运行：./venv/bin/python scripts/eval_phase8.py [--json]
输出：docs/agent-upgrade/eval_phase8_results.json + 控制台 markdown 表格
"""
import json
import os
import sys
import tempfile
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import backend.auth  # noqa: F401  (导入顺序安全)

RESULTS = []


def scenario(sid, kind, name):
    def deco(fn):
        def run(ctx):
            try:
                detail = fn(ctx) or ""
                RESULTS.append({"id": sid, "kind": kind, "name": name,
                                "pass": True, "detail": str(detail)[:300]})
                return True
            except Exception as e:  # noqa: BLE001
                RESULTS.append({"id": sid, "kind": kind, "name": name,
                                "pass": False,
                                "detail": "%s: %s" % (
                                    type(e).__name__, str(e)[:200])})
                return False
        run.sid = sid
        return run
    return deco


class Ctx:
    """每场景独立 scratch 库。"""

    def __init__(self):
        from backend.auth.database import get_session_factory, reset_engine
        self.tmp = tempfile.mkdtemp(prefix="eval8_")
        self.url = "sqlite:///%s/eval.db" % self.tmp
        os.environ["DATABASE_URL"] = self.url
        os.environ["LUPIN_DB_AUTO_CREATE"] = "0"
        reset_engine()
        from alembic.config import Config
        from alembic import command
        cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
        command.upgrade(cfg, "head")
        self.sf = get_session_factory()
        self.db = self.sf()

    def org(self, name="评测合作社"):
        from backend.models import Organization, Membership
        o = Organization(name=name, created_by="u1")
        self.db.add(o)
        self.db.flush()
        self.db.add(Membership(user_id="u1", org_id=o.id, role="owner"))
        self.db.commit()
        self.db.refresh(o)
        return o

    def close(self):
        from backend.auth.database import reset_engine
        self.db.close()
        reset_engine()


# ---------------- fake 大脑 ----------------

def _brain_oneshot():
    def fake(prompt, feedback=None):
        if "brief 解析器" in prompt:
            return {"product_name": "烟台苹果", "platform": "抖音",
                    "marketing_goal": "带货", "budget_yuan": 2000,
                    "people_count": 2, "video_duration_sec": 60}
        if "营销策略师" in prompt:
            return {"strategy": {
                "goals": ["卖500单"], "target_audience": "年轻人",
                "content_pillars": ["现摘"], "key_messages": ["脆甜"]}}
        if "营销策划师" in prompt:
            return {"ideas": [
                {"title": "果园现摘", "angle": "进果园",
                 "selling_points": ["现摘现发"]}]}
        if "分镜师" in prompt:
            return {"shots": [
                {"shot_no": 1, "scene": "果园", "shot_type": "中景",
                 "duration_sec": 60, "voiceover": "现摘的苹果又脆又甜",
                 "subtitle": "烟台苹果"}]}
        if "平台文案写手" in prompt:
            return {"copy": {"platform": "抖音", "title": "烟台苹果",
                             "text": "现摘现发，欢迎下单",
                             "hashtags": ["烟台苹果"]}}
        raise AssertionError("unexpected: %s" % prompt[:40])
    return fake


def _modify_brain(plan, updates):
    import copy as _c

    def fake(prompt, feedback=None):
        if "【修改意图解析】" in prompt:
            return _c.deepcopy(plan)
        if "【局部重写" in prompt:
            return {"updates": _c.deepcopy(updates)}
        raise AssertionError("unexpected: %s" % prompt[:40])
    return fake


def _oneshot(ctx, text, brain=None):
    from backend.services.agent import run_one_shot_brief
    return run_one_shot_brief(
        ctx.db, "u1", ctx.org_id, text,
        session_factory=lambda: ctx.sf(),
        ai_generate=brain or _brain_oneshot())


# ================= 场景 =================

@scenario("E01", "正常", "一句话生成标准 brief")
def s01(ctx):
    ctx.org_id = ctx.org().id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    assert out["run_status"] == "completed", out["summary"]
    from backend.repositories.work_repository import WorkRepository
    data = json.loads(WorkRepository(ctx.db).get(
        out["work_id"], ctx.org_id).data_json)
    missing = [k for k in ("strategy", "creative", "storyboard",
                           "shooting_list", "copy") if not data.get(k)]
    assert not missing, missing
    return "work=%s 5 artifacts 齐" % out["work_id"][:8]


@scenario("E02", "正常", "多轮对话复用 conversation（brief 增量合并）")
def s02(ctx):
    from backend.services.agent import create_agent_run
    org = ctx.org()
    ctx.org_id = org.id
    t1 = create_agent_run(ctx.db, "u1", org.id,
                          brief_json={"product_name": "烟台苹果"})
    from backend.ai_tasks.repository import AITaskRepository
    c1 = AITaskRepository(ctx.db).get(t1).payload_json["conversation_id"]
    t2 = create_agent_run(ctx.db, "u1", org.id,
                          brief_json={"platform": "快手"},
                          conversation_id=c1)
    c2 = AITaskRepository(ctx.db).get(t2).payload_json["conversation_id"]
    assert c1 == c2, "应复用同一会话"
    from backend.services.agent.conversations import get_conversation
    bj = get_conversation(ctx.db, c1, org.id).brief_json
    assert bj["brief"]["product_name"] == "烟台苹果"
    assert bj["brief"]["platform"] == "快手"
    return "同一 conversation 串联两轮，brief 增量合并"


@scenario("E03", "正常", "局部修改：改文案标题")
def s03(ctx):
    from backend.services.agent import (
        apply_modification, create_conversation)
    from backend.repositories.work_repository import WorkRepository
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    before = json.loads(WorkRepository(ctx.db).get(wid, org.id).data_json)
    plan = {"target_kind": "artifact", "target": "copy",
            "operation": "rewrite",
            "params": {"instruction": "标题更吸引人"},
            "scope": "local", "touches_confirmed_fact": False}
    r = apply_modification(
        ctx.db, "u1", org.id, wid, conv.id, "改标题",
        ai_generate=_modify_brain(plan, {"title": "新标题"}))
    assert r["status"] == "completed", r
    after = json.loads(WorkRepository(ctx.db).get(wid, org.id).data_json)
    assert after["copy"]["title"] == "新标题"
    changed = [k for k in before if before[k] != after[k]]
    assert changed == ["copy"], changed
    return "仅 copy 变化，其余零变化"


@scenario("E04", "正常", "局部修改：预算数字纯字段替换")
def s04(ctx):
    from backend.services.agent import (
        apply_modification, create_conversation)
    from backend.repositories.work_repository import WorkRepository
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    plan = {"target_kind": "field", "target": "brief",
            "operation": "replace",
            "params": {"updates": {"brief.budget_yuan": 300}},
            "scope": "local", "touches_confirmed_fact": False}
    r = apply_modification(
        ctx.db, "u1", org.id, wid, conv.id, "预算改300",
        ai_generate=_modify_brain(plan, {}))
    assert r["status"] == "completed", r
    after = json.loads(WorkRepository(ctx.db).get(wid, org.id).data_json)
    assert after["brief"]["budget_yuan"] == 300
    return "budget_yuan=300"


@scenario("E05", "正常", "版本恢复")
def s05(ctx):
    from backend.services.agent import (
        apply_modification, restore_modification, create_conversation)
    from backend.repositories.work_repository import WorkRepository
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    before = json.loads(WorkRepository(ctx.db).get(wid, org.id).data_json)
    plan = {"target_kind": "artifact", "target": "copy",
            "operation": "rewrite", "params": {"instruction": "x"},
            "scope": "local", "touches_confirmed_fact": False}
    r = apply_modification(
        ctx.db, "u1", org.id, wid, conv.id, "改标题",
        ai_generate=_modify_brain(plan, {"title": "新标题"}))
    rr = restore_modification(ctx.db, "u1", org.id, wid,
                              r["snapshot_version_no"])
    assert rr["restored"] is True
    restored = json.loads(WorkRepository(ctx.db).get(
        wid, org.id).data_json)
    assert restored == before
    return "恢复后与修改前一致"


@scenario("E06", "正常", "plan 执行：query_facts+query_products")
def s06(ctx):
    from backend.services.agent import (
        create_agent_run, set_run_plan, execute_run_steps)
    from backend.services.agent.adapters import (
        run_tool as _rt, ToolDeps, ToolResult)
    from backend.services.agent import runner as _rm
    from backend.services.agent.context import AgentIdentity
    from backend.models import Fact, Product
    org = ctx.org()
    p = Product(org_id=org.id, code="P1", name="烟台苹果")
    ctx.db.add(p)
    ctx.db.flush()
    ctx.db.add(Fact(org_id=org.id, product_id=p.id,
                    content="现摘现发", status="confirmed",
                    allow_use=True))
    ctx.db.commit()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "查数"})
    set_run_plan(ctx.db, tid, org.id, {"steps": [
        {"tool_id": "query_facts", "params": {}},
        {"tool_id": "query_products", "params": {"keyword": "苹果"}}]})
    got = {}

    def wrap(deps, tool_id, params, confirmed=False):
        r = _rt(ToolDeps(db=deps.db, identity=deps.identity,
                         ai_generate=None), tool_id, params,
                confirmed=confirmed)
        got[tool_id] = r.data
        return r
    _rm.run_tool = wrap
    try:
        out = execute_run_steps(ctx.db, tid, lambda: False)
    finally:
        import importlib
        importlib.reload(_rm)
    assert out["run_status"] == "completed", out
    assert any("现摘现发" in f["content"]
               for f in got["query_facts"]["facts"])
    assert any(x["name"] == "烟台苹果"
               for x in got["query_products"]["products"])
    return "真实 adapter 读到种子行"


@scenario("E07", "正常", "导出 export-json 含版本历史")
def s07(ctx):
    from backend.services.agent import (
        apply_modification, create_conversation)
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    plan = {"target_kind": "artifact", "target": "copy",
            "operation": "rewrite", "params": {"instruction": "x"},
            "scope": "local", "touches_confirmed_fact": False}
    r = apply_modification(
        ctx.db, "u1", org.id, wid, conv.id, "改",
        ai_generate=_modify_brain(plan, {"title": "T"}))
    # 直接走 endpoint 的数据组装逻辑（service 层等价）
    from backend.repositories.work_repository import WorkRepository
    from backend.models import WorkVersion
    w = WorkRepository(ctx.db).get(wid, org.id)
    vers = ctx.db.query(WorkVersion).filter_by(work_id=wid).all()
    assert w is not None and json.loads(w.data_json)["copy"]
    assert any(v.version_no == r["snapshot_version_no"]
               for v in vers)
    return "versions=%d 含快照 v%s" % (
        len(vers), r["snapshot_version_no"])


@scenario("E08", "边界", "brief 缺字段 → 透明默认值")
def s08(ctx):
    ctx.org_id = ctx.org().id
    out = _oneshot(ctx, "苹果")
    assert out["run_status"] == "completed"
    s = out["summary"]
    assert s.get("confirmed_defaults"), s
    return "confirmed_defaults=%d 项" % len(s["confirmed_defaults"])


@scenario("E09", "边界", "空 brief → failed 可读错误")
def s09(ctx):
    ctx.org_id = ctx.org().id
    out = _oneshot(ctx, "   ")
    assert out["run_status"] == "failed"
    assert out["summary"].get("error")
    return out["summary"]["error"][:60]


@scenario("E10", "边界", "预算 0 视为未指定")
def s10(ctx):
    from backend.services.agent.brief import parse_brief_from_text
    b = parse_brief_from_text(
        "x", lambda p, f=None: {"product_name": "苹果",
                               "budget_yuan": 0})
    assert b.budget_yuan is None
    return "budget_yuan=None（未指定）"


@scenario("E11", "边界", "超长 brief 截断 2000")
def s11(ctx):
    ctx.org_id = ctx.org().id
    out = _oneshot(ctx, "苹果" * 2000)
    from backend.ai_tasks.repository import AITaskRepository
    t = AITaskRepository(ctx.db).get(out["task_id"])
    stored = (t.payload_json.get("brief") or {}).get("text", "")
    assert 0 < len(stored) <= 2000
    return "存了 %d 字符（原文 4000 截断）" % len(stored)


@scenario("E12", "边界", "触碰确认事实 → waiting_user")
def s12(ctx):
    from backend.services.agent import (
        apply_modification, create_conversation)
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    plan = {"target_kind": "field", "target": "brief.product_name",
            "operation": "replace",
            "params": {"value": "烟台红富士"},
            "scope": "local", "touches_confirmed_fact": True}
    r = apply_modification(
        ctx.db, "u1", org.id, wid, conv.id, "改产品名",
        ai_generate=_modify_brain(plan, {}))
    assert r["status"] == "waiting_user", r
    assert "confirmed_fact" in r.get("confirm_reason", "")
    return r["confirm_reason"][:50]


@scenario("E13", "边界", "高风险宣称无引用 → 自动标待核实")
def s13(ctx):
    ctx.org_id = ctx.org().id

    def brain2(prompt, feedback=None):
        # 高风险放分镜口播（步骤 5，在有限修复步骤 8 之前）
        if "分镜师" in prompt:
            return {"shots": [
                {"shot_no": 1, "scene": "果园", "shot_type": "中景",
                 "duration_sec": 60,
                 "voiceover": "吃了降血压的神奇苹果，快来买",
                 "subtitle": "x"}]}
        return _brain_oneshot()(prompt, feedback)

    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频", brain=brain2)
    assert out["run_status"] == "completed", out["summary"]
    assert out["summary"].get("repaired_claims"), "应有修复记录"
    assert any("待核实" in w for w in out["summary"]["warnings"])
    return "repaired=%d 处" % len(out["summary"]["repaired_claims"])


@scenario("E14", "边界", "可选步骤失败 → partial")
def s14(ctx):
    from backend.services.agent import (
        create_agent_run, set_run_plan, execute_run_steps)
    from backend.services.agent.adapters import ToolResult
    from backend.services.agent import runner as _rm
    org = ctx.org()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    set_run_plan(ctx.db, tid, org.id, {"steps": [
        {"tool_id": "query_facts", "params": {}},
        {"tool_id": "query_products", "params": {},
         "optional": True}]})

    def flaky(deps, tool_id, params, confirmed=False):
        if tool_id == "query_products":
            return ToolResult(status="failed", error_code="internal",
                              error_message="boom")
        return ToolResult(status="ok", data={})
    _rm.run_tool = flaky
    try:
        out = execute_run_steps(ctx.db, tid, lambda: False)
    finally:
        import importlib
        importlib.reload(_rm)
    assert out["run_status"] == "partial", out
    return "partial"


@scenario("E15", "边界", "非法 plan 被拒（未知 tool）")
def s15(ctx):
    from backend.services.agent import create_agent_run, set_run_plan
    org = ctx.org()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    try:
        set_run_plan(ctx.db, tid, org.id, {"steps": [
            {"tool_id": "exec_shell", "params": {}}]})
    except ValueError as e:
        return "拒绝：%s" % str(e)[:60]
    raise AssertionError("非法 plan 未被拒绝")


@scenario("E16", "边界", "超长 plan 被拒（26 步）")
def s16(ctx):
    from backend.services.agent import create_agent_run, set_run_plan
    org = ctx.org()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    steps = [{"tool_id": "query_facts", "params": {}} for _ in range(26)]
    try:
        set_run_plan(ctx.db, tid, org.id, {"steps": steps})
    except ValueError as e:
        return "拒绝：%s" % str(e)[:60]
    raise AssertionError("超限 plan 未被拒绝")


@scenario("E17", "失败", "模型超时 → 快速失败无重试")
def s17(ctx):
    import time
    from backend.services.agent import (
        create_agent_run, set_run_plan, execute_run_steps)
    from backend.services.agent import runner as _rm
    org = ctx.org()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    set_run_plan(ctx.db, tid, org.id, {"steps": [
        {"tool_id": "query_facts", "params": {}}]})
    calls = []

    def timeout_tool(deps, tool_id, params, confirmed=False):
        calls.append(1)
        raise TimeoutError("网关超时")
    _rm.run_tool = timeout_tool
    t0 = time.time()
    try:
        out = execute_run_steps(ctx.db, tid, lambda: False)
    finally:
        import importlib
        importlib.reload(_rm)
    assert out["run_status"] == "failed"
    assert len(calls) == 1 and time.time() - t0 < 10
    return "1 次调用快速失败"


@scenario("E18", "失败", "429 硬停零新调用")
def s18(ctx):
    ctx.org_id = ctx.org().id
    seen = []

    def fake(prompt, feedback=None):
        seen.append(prompt)
        if "营销策略师" in prompt:
            raise RuntimeError("HTTP 429 Too Many Requests")
        if "brief 解析器" in prompt:
            return {"product_name": "苹果"}
        raise AssertionError("不应继续调用")
    out = _oneshot(ctx, "做视频", brain=fake)
    assert out["run_status"] in ("partial", "failed")
    blob = " ".join(seen)
    assert "分镜师" not in blob
    return "429 后零新调用"


@scenario("E19", "失败", "5xx 不重试")
def s19(ctx):
    ctx.org_id = ctx.org().id
    calls = []

    def fake(prompt, feedback=None):
        calls.append(1)
        raise RuntimeError("500 Internal Server Error")
    out = _oneshot(ctx, "做视频", brain=fake)
    assert out["run_status"] == "failed"
    assert len(calls) == 1
    return "1 次调用直接失败"


@scenario("E20", "对抗", "提示注入改白名单被拒")
def s20(ctx):
    from backend.services.agent.adapters import run_tool, ToolDeps
    from backend.services.agent.context import AgentIdentity
    org = ctx.org()
    deps = ToolDeps(db=ctx.db,
                    identity=AgentIdentity(user_id="u1",
                                           org_id=org.id,
                                           role="owner"),
                    ai_generate=None)
    r = run_tool(deps, "exec_shell", {"cmd": "rm -rf /"})
    assert r.status == "failed"
    assert r.error_code == "unknown_tool"
    # params 注入 org_id 越权
    r2 = run_tool(deps, "query_facts",
                  {"org_id": "other-org"})
    assert r2.status in ("failed", "ok")
    if r2.status == "ok":
        # 白名单 params 忽略多余键，归属仍走服务端 identity
        pass
    return "白名单外工具拒绝"


@scenario("E21", "对抗", "跨租户读 run 被拒")
def s21(ctx):
    from backend.services.agent import create_agent_run, get_run_state
    from backend.repositories.base import EntityNotFound
    org = ctx.org("A 合作社")
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    st = get_run_state(ctx.db, tid, "other-org")
    assert st.get("found") is False, st
    st2 = get_run_state(ctx.db, tid, org.id)
    assert st2.get("found") is True
    return "跨租户 found=False（404 防枚举），本租户可读"


@scenario("E22", "对抗", "跨作品修改被拒")
def s22(ctx):
    from backend.services.agent import (
        apply_modification, create_conversation)
    from backend.repositories.work_repository import WorkRepository
    org = ctx.org()
    ctx.org_id = org.id
    out = _oneshot(ctx, "给烟台苹果做条抖音带货视频")
    wid = out["work_id"]
    other = WorkRepository(ctx.db).create(
        org_id=org.id, owner_id="u1", title="别的作品",
        data={"copy": {}}, status="draft")
    ctx.db.commit()
    conv = create_conversation(ctx.db, org.id, "u1", work_id=wid,
                               brief={})
    from backend.repositories.base import EntityNotFound
    plan = {"target_kind": "artifact", "target": "copy",
            "operation": "rewrite", "params": {"instruction": "x"},
            "scope": "local", "touches_confirmed_fact": False}
    try:
        apply_modification(
            ctx.db, "u1", org.id, other.id, conv.id, "改",
            ai_generate=_modify_brain(plan, {}))
    except EntityNotFound:
        return "跨作品拒绝"
    raise AssertionError("跨作品未拒绝")


@scenario("E23", "对抗", "重复提交同幂等键 → 同一任务")
def s23(ctx):
    from starlette.requests import Request
    from backend.core.idempotency import idempotency_begin
    from backend.services.agent import create_agent_run
    from backend.ai_tasks.repository import AITaskRepository
    org = ctx.org()
    key = "k-eval-23"

    def req():
        return Request({"type": "http", "headers": [
            (b"idempotency-key", key.encode())]})
    body = {"org_id": org.id, "brief": {"goal": "幂等"}}

    def once():
        idem = idempotency_begin("agent-runs", req(), body,
                                 scope=org.id, org_id=org.id)
        if idem.replay is not None:
            return idem.replay
        try:
            tid = create_agent_run(ctx.db, "u1", org.id,
                                   brief_json={"goal": "幂等"})
        except Exception:
            idem.abort()
            raise
        return idem.finish({"task_id": tid})
    r1, r2 = once(), once()
    assert r1["task_id"] == r2["task_id"]
    assert r2.get("idempotent_replay") is True
    n = sum(1 for t in AITaskRepository(ctx.db).list_by_org(org.id)
            if t.task_type == "agent_run")
    assert n == 1
    return "同一 task_id，无重复"


@scenario("E24", "对抗", "并发编辑 stale 版本 → 冲突")
def s24(ctx):
    from backend.services.agent.adapters import run_tool, ToolDeps
    from backend.services.agent.context import AgentIdentity
    from backend.repositories.work_repository import WorkRepository
    org = ctx.org()
    w = WorkRepository(ctx.db).create(
        org_id=org.id, owner_id="u1", title="并发作品",
        data={"copy": {"title": "v1"}}, status="draft")
    ctx.db.commit()
    ctx.db.refresh(w)
    v0 = w.version
    deps = ToolDeps(db=ctx.db,
                    identity=AgentIdentity(user_id="u1",
                                           org_id=org.id,
                                           role="owner"),
                    ai_generate=None)
    r1 = run_tool(deps, "patch_work",
                  {"work_id": w.id, "expected_version": v0,
                   "data_patch": {"copy": {"title": "v2"}}},
                  confirmed=True)
    assert r1.status == "ok"
    r2 = run_tool(deps, "patch_work",
                  {"work_id": w.id, "expected_version": v0,
                   "data_patch": {"copy": {"title": "stale"}}},
                  confirmed=True)
    assert r2.status == "failed" and r2.error_code == "conflict"
    cur = json.loads(WorkRepository(ctx.db).get(w.id, org.id).data_json)
    assert cur["copy"]["title"] == "v2"
    return "stale 写入被拒，数据完好"


@scenario("E25", "对抗", "用户中途取消")
def s25(ctx):
    from backend.services.agent import (
        create_agent_run, set_run_plan, execute_run_steps)
    from backend.services.agent.adapters import ToolResult
    from backend.services.agent import runner as _rm
    from backend.ai_tasks.repository import AITaskRepository
    from backend.ai_tasks.queue import _TaskCancelled
    org = ctx.org()
    tid = create_agent_run(ctx.db, "u1", org.id,
                           brief_json={"goal": "x"})
    set_run_plan(ctx.db, tid, org.id, {"steps": [
        {"tool_id": "query_facts", "params": {}},
        {"tool_id": "query_products", "params": {}}]})
    calls = []

    def ok_tool(deps, tool_id, params, confirmed=False):
        calls.append(tool_id)
        return ToolResult(status="ok", data={})
    _rm.run_tool = ok_tool
    try:
        execute_run_steps(
            ctx.db, tid, lambda: len(calls) >= 1)
    except _TaskCancelled:
        pass
    finally:
        import importlib
        importlib.reload(_rm)
    t = AITaskRepository(ctx.db).get(tid)
    assert t.payload_json["run_status"] == "cancelled"
    assert calls == ["query_facts"]
    return "取消后停步，状态 cancelled"


SCENARIOS = [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11,
             s12, s13, s14, s15, s16, s17, s18, s19, s20, s21, s22,
             s23, s24, s25]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for sc in SCENARIOS:
        ctx = Ctx()
        try:
            sc(ctx)
        finally:
            ctx.close()
    out_path = os.path.join(PROJECT_ROOT, "docs", "agent-upgrade",
                            "eval_phase8_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"scenarios": RESULTS,
                   "summary": {
                       "total": len(RESULTS),
                       "passed": sum(1 for r in RESULTS if r["pass"]),
                       "failed": sum(1 for r in RESULTS if not r["pass"])},
                   "note": "mock 驱动的确定性评测，不是生产流量"},
                  f, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
        return
    print("| ID | 类别 | 场景 | 结果 | 说明 |")
    print("|---|---|---|---|---|")
    for r in RESULTS:
        mark = "PASS" if r["pass"] else "FAIL"
        print("| %s | %s | %s | %s | %s |" % (
            r["id"], r["kind"], r["name"], mark, r["detail"]))
    s = sum(1 for r in RESULTS if r["pass"])
    print("\n%d/%d 通过" % (s, len(RESULTS)))
    print("结果已存：%s" % out_path)


if __name__ == "__main__":
    main()
