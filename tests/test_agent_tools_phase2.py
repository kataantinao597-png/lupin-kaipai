"""Phase 2 测试：Agent 工具层（tools/context/adapters/planner）+ 0020 迁移。

- 工具 schema 单测（合法/非法输入）
- 白名单外 tool_id 拒绝（run_tool + planner）
- 权限拒绝（伪造 org_id / 跨组织 work_id / 角色不足）
- 异常转结构化错误（error_code 信封，不抛原始异常）
- 0020 迁移：空库 upgrade/downgrade/upgrade 全周期；scratch 拷贝库可执行；
  真实业务库 backend/data/lupin.db 只读，绝不写入/迁移。

运行：./venv/bin/python -m pytest tests/test_agent_tools_phase2.py
"""
import hashlib
import json
import os
import shutil
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
from backend.services.agent import (  # noqa: E402
    AgentIdentity,
    ToolDeps,
    ToolTier,
    TOOLS_REGISTRY,
    assemble_agent_context,
    check_user_quota,
    get_tool,
    is_tool_allowed,
    run_tool,
    tools_needing_model,
    tools_requiring_confirmation,
    validate_plan,
    validate_tool_params,
)
from backend.services.agent.tools import ToolParamError  # noqa: E402
from backend import models  # noqa: E402

REAL_DB = os.path.join(PROJECT_ROOT, "backend", "data", "lupin.db")


# ---------------------------------------------------------------------------
# 共享 DB：模块级升级一次，测试间按 org 隔离数据
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("agentdb") / "agent.db")
    url = f"sqlite:///{path}"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["LUPIN_DB_AUTO_CREATE"] = "0"
    reset_engine()
    try:
        from alembic.config import Config
        from alembic import command
        cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
        command.upgrade(cfg, "head")
        yield path
    finally:
        reset_engine()
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


@pytest.fixture()
def db(db_path):
    session = get_session_factory()()
    yield session
    session.close()


_org_seq = [0]


def _mk_org(db, role="editor"):
    """建组织 + 成员，返回 (org_id, identity)。"""
    _org_seq[0] += 1
    n = _org_seq[0]
    org = models.Organization(name=f"agent测试合作社{n}", created_by="u_agent")
    db.add(org)
    db.flush()
    user_id = f"u_agent_{n}"
    ms = models.Membership(user_id=user_id, org_id=org.id, role=role)
    db.add(ms)
    db.commit()
    return org.id, AgentIdentity(user_id=user_id, org_id=org.id, role=role)


def _deps(db, identity, **kw):
    return ToolDeps(db=db, identity=identity, **kw)


# ---------------------------------------------------------------------------
# 1. 工具 schema 单测
# ---------------------------------------------------------------------------

class TestToolSchemas:
    def test_registry_has_16_tools(self):
        assert len(TOOLS_REGISTRY) == 16
        assert set(TOOLS_REGISTRY) == {
            "query_facts", "query_products", "generate_ideas",
            "generate_storyboard", "generate_strategy",
            "generate_platform_copy",
            "check_risk", "shooting_checklist",
            "rewrite_copy", "check_delivery", "parse_brief", "patch_work",
            "get_task_status", "query_usage", "regenerate_shot",
            "delete_work",
        }

    def test_tiers_cover_all(self):
        tiers = {t.tier for t in TOOLS_REGISTRY.values()}
        assert tiers == {ToolTier.CORE_REQUIRED, ToolTier.OPTIONAL,
                         ToolTier.USER_CONFIRMATION_REQUIRED}
        assert set(tools_requiring_confirmation()) == {"patch_work",
                                                       "delete_work"}
        assert set(tools_needing_model()) == {
            "generate_ideas", "generate_storyboard", "regenerate_shot",
            "rewrite_copy", "parse_brief", "generate_strategy",
            "generate_platform_copy",
        }

    def test_valid_params_pass(self):
        cases = {
            "query_facts": {"limit": 10},
            "query_products": {"keyword": "苹果"},
            "generate_ideas": {"brief": "给烟台苹果做一条短视频"},
            "generate_storyboard": {"brief": "苹果短视频", "shot_count": 5},
            "generate_strategy": {"brief": "给烟台苹果做一条短视频"},
            "generate_platform_copy": {"brief": "苹果短视频",
                                       "platform": "抖音"},
            "check_risk": {"storyboard": {"shots": []}},
            "shooting_checklist": {"storyboard": {"shots": []}},
            "rewrite_copy": {"text": "苹果很好吃"},
            "check_delivery": {"check_type": "plan"},
            "parse_brief": {"user_text": "帮我做个视频"},
            "patch_work": {"work_id": "w1", "expected_version": 1,
                           "title": "新标题"},
            "get_task_status": {"task_id": "t1"},
            "query_usage": {"limit": 10},
            "regenerate_shot": {"work_id": "w1", "storyboard_id": "s1",
                                "shot_no": 2, "instruction": "换个角度"},
            "delete_work": {"work_id": "w1"},
        }
        for tool_id, params in cases.items():
            validate_tool_params(tool_id, params)  # 不抛即过

    def test_invalid_params_rejected(self):
        with pytest.raises(ToolParamError):
            validate_tool_params("query_facts", {"limit": 999})  # >200
        with pytest.raises(ToolParamError):
            validate_tool_params("patch_work", {"expected_version": 1})  # 缺 work_id
        with pytest.raises(ToolParamError):
            validate_tool_params("generate_storyboard", {"shot_count": 0})  # <1
        with pytest.raises(ToolParamError):
            validate_tool_params("rewrite_copy", {"text": ""})  # 空文本
        with pytest.raises(ToolParamError):
            validate_tool_params("check_delivery", {"check_type": "nope"})  # 非枚举
        with pytest.raises(ToolParamError):
            validate_tool_params("regenerate_shot",
                                 {"work_id": "w", "storyboard_id": "s",
                                  "shot_no": 1, "instruction": "x" * 1001})  # 超长

    def test_extra_fields_rejected(self):
        # 拒绝未知字段：防止模型往 params 里塞私货
        with pytest.raises(ToolParamError):
            validate_tool_params("query_facts",
                                 {"limit": 10, "drop_table": True})
        with pytest.raises(ToolParamError):
            validate_tool_params("patch_work",
                                 {"work_id": "w", "expected_version": 1,
                                  "title": "t", "__proto__": 1})

    def test_unknown_tool_keyerror(self):
        assert not is_tool_allowed("rm_rf")
        assert get_tool("rm_rf") is None
        with pytest.raises(KeyError):
            validate_tool_params("rm_rf", {})


# ---------------------------------------------------------------------------
# 2. planner：白名单 / 结构 / 上限
# ---------------------------------------------------------------------------

class TestPlanner:
    def test_rejects_unknown_tool(self):
        v = validate_plan({"steps": [{"tool_id": "evil_tool",
                                      "params": {}}]})
        assert not v.ok and any("白名单" in e for e in v.errors)

    def test_rejects_bad_shapes(self):
        assert not validate_plan("nope").ok
        assert not validate_plan({"steps": []}).ok
        v = validate_plan({"steps": [{"tool_id": "query_facts",
                                      "params": {"limit": -1}}]})
        assert not v.ok
        # params 缺省为 {} 时合法（全字段有默认值）
        v2 = validate_plan({"steps": [{"tool_id": "query_facts"}]})
        assert v2.ok, v2.errors

    def test_rejects_bad_depends(self):
        v = validate_plan({"steps": [
            {"tool_id": "query_facts", "params": {},
             "depends_on": [5]}]})
        assert not v.ok
        v = validate_plan({"steps": [
            {"tool_id": "query_facts", "params": {},
             "depends_on": [0]}]})
        assert not v.ok  # 自依赖

    def test_rejects_cycle(self):
        v = validate_plan({"steps": [
            {"tool_id": "query_facts", "params": {}, "depends_on": [1]},
            {"tool_id": "query_products", "params": {}, "depends_on": [0]},
        ]})
        assert not v.ok and any("循环" in e for e in v.errors)

    def test_rejects_too_many_steps(self):
        v = validate_plan({"steps": [
            {"tool_id": "query_facts", "params": {}}] * 26})
        assert not v.ok and any("上限" in e for e in v.errors)

    def test_rejects_too_many_model_steps(self):
        v = validate_plan({"steps": [
            {"tool_id": "parse_brief", "params": {"user_text": "x"}}] * 9})
        assert not v.ok and any("模型调用" in e for e in v.errors)

    def test_valid_plan_flags_confirmation(self):
        v = validate_plan({"steps": [
            {"tool_id": "query_facts", "params": {"limit": 5}},
            {"tool_id": "patch_work",
             "params": {"work_id": "w1", "expected_version": 2,
                        "title": "t"}, "depends_on": [0]},
            {"tool_id": "delete_work", "params": {"work_id": "w2"},
             "depends_on": [1]},
        ]})
        assert v.ok, v.errors
        assert v.needs_confirmation == [1, 2]
        assert v.model_steps == []


# ---------------------------------------------------------------------------
# 3. 权限拒绝：伪造 org_id / 跨组织 / 角色不足
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_forged_org_id_forbidden(self, db):
        # 用户只属于 orgA，却伪造 identity.org_id=orgB → 无成员关系 → forbidden
        org_a, ident_a = _mk_org(db, role="editor")
        deps = _deps(db, AgentIdentity(user_id=ident_a.user_id,
                                      org_id="org_not_exist",
                                      role="editor"))
        r = run_tool(deps, "query_facts", {})
        assert r.status == "failed" and r.error_code == "forbidden"

    def test_cross_org_work_not_found(self, db):
        # 用户同时是 A/B 成员；用 orgB 身份读 orgA 的作品 → not_found（防枚举）
        org_a, ident_a = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_a, ident_a.user_id, title="A的作品")
        db.commit()
        org_b, ident_b = _mk_org(db, role="editor")
        r = run_tool(_deps(db, ident_b), "patch_work",
                     {"work_id": w.id, "expected_version": 1,
                      "title": "篡改"}, confirmed=True)
        assert r.status == "failed" and r.error_code == "not_found"
        # 另一组织的 get_task_status 同理
        r2 = run_tool(_deps(db, ident_b), "query_facts", {})
        assert r2.status == "ok"  # 同组织读事实不受影响

    def test_viewer_cannot_patch(self, db):
        org_id, ident = _mk_org(db, role="viewer")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="v")
        db.commit()
        r = run_tool(_deps(db, ident), "patch_work",
                     {"work_id": w.id, "expected_version": 1,
                      "title": "x"}, confirmed=True)
        assert r.status == "failed" and r.error_code == "forbidden"

    def test_viewer_can_read(self, db):
        org_id, ident = _mk_org(db, role="viewer")
        r = run_tool(_deps(db, ident), "query_facts", {"limit": 5})
        assert r.status == "ok" and r.error_code is None

    def test_needs_confirmation_gate(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="t")
        db.commit()
        r = run_tool(_deps(db, ident), "patch_work",
                     {"work_id": w.id, "expected_version": 1, "title": "x"},
                     confirmed=False)
        assert r.status == "failed"
        assert r.error_code == "needs_confirmation"
        r2 = run_tool(_deps(db, ident), "delete_work", {"work_id": w.id},
                      confirmed=False)
        assert r2.error_code == "needs_confirmation"


# ---------------------------------------------------------------------------
# 4. 适配器：正常路径 / 异常转结构化错误
# ---------------------------------------------------------------------------

class TestAdapters:
    def test_query_facts_usable_only(self, db):
        org_id, ident = _mk_org(db, role="editor")
        f1 = models.Fact(org_id=org_id, content="已确认事实", status="confirmed",
                         allow_use=True)
        f2 = models.Fact(org_id=org_id, content="待审核事实", status="pending",
                         allow_use=True)
        db.add_all([f1, f2])
        db.commit()
        r = run_tool(_deps(db, ident), "query_facts", {})
        assert r.status == "ok"
        assert r.data["count"] == 1
        assert r.data["facts"][0]["content"] == "已确认事实"

    def test_query_products(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.product_repository import ProductRepository
        ProductRepository(db).create(org_id, code="P001", name="烟台苹果")
        db.commit()
        r = run_tool(_deps(db, ident), "query_products", {"code": "P001"})
        assert r.status == "ok" and r.data["count"] == 1
        r2 = run_tool(_deps(db, ident), "query_products",
                      {"product_id": "not_exist"})
        assert r2.error_code == "not_found"

    def test_patch_work_ok_and_conflict(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="旧标题",
                                      data={"brief": "b"})
        db.commit()
        deps = _deps(db, ident)
        r = run_tool(deps, "patch_work",
                     {"work_id": w.id, "expected_version": 1,
                      "title": "新标题",
                      "data_patch": {"shoot_notes": "带三脚架"}},
                     confirmed=True)
        assert r.status == "ok", r.error_message
        assert r.data["version"] == 2
        assert r.artifact_ref == f"work:{w.id}:v2"
        # 局部语义：brief 保留，shoot_notes 新增
        w2 = WorkRepository(db).get(w.id, org_id)
        data = json.loads(w2.data_json)
        assert data["brief"] == "b" and data["shoot_notes"] == "带三脚架"
        # 旧版本号 → 乐观锁冲突
        r2 = run_tool(deps, "patch_work",
                      {"work_id": w.id, "expected_version": 1,
                       "title": "再改"}, confirmed=True)
        assert r2.status == "failed" and r2.error_code == "conflict"

    def test_patch_work_illegal_key(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="t")
        db.commit()
        r = run_tool(_deps(db, ident), "patch_work",
                     {"work_id": w.id, "expected_version": 1,
                      "data_patch": {"owner_id": "hacker"}}, confirmed=True)
        assert r.status == "failed" and r.error_code == "invalid_params"

    def test_delete_work_soft(self, db):
        org_id, ident = _mk_org(db, role="admin")  # editor 无 work:delete
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="删我")
        db.commit()
        r = run_tool(_deps(db, ident), "delete_work", {"work_id": w.id},
                     confirmed=True)
        assert r.status == "ok" and r.data["deleted"] is True
        w2 = db.get(models.Work, w.id)
        assert w2.deleted_at is not None

    def test_generate_ideas_needs_model(self, db):
        org_id, ident = _mk_org(db, role="editor")
        r = run_tool(_deps(db, ident), "generate_ideas",
                     {"brief": "苹果视频"})
        assert r.status == "failed" and r.error_code == "needs_model"

    def test_generate_ideas_with_fake_ai(self, db):
        org_id, ident = _mk_org(db, role="editor")
        fake = lambda prompt: {"ideas": [  # noqa: E731
            {"title": "果园直拍", "angle": "真实", "selling_points": ["现摘"]}]}
        r = run_tool(_deps(db, ident, ai_generate=fake),
                     "generate_ideas", {"brief": "苹果视频"})
        assert r.status == "ok"
        assert r.data["ideas"][0]["title"] == "果园直拍"

    def test_model_exception_structured(self, db):
        org_id, ident = _mk_org(db, role="editor")

        def boom(prompt):
            raise RuntimeError("上游炸了")

        r = run_tool(_deps(db, ident, ai_generate=boom),
                     "generate_ideas", {"brief": "x"})
        assert r.status == "failed" and r.error_code == "model_error"
        assert "上游炸了" in (r.error_message or "")

    def test_check_risk_deterministic(self, db):
        org_id, ident = _mk_org(db, role="editor")
        sb = {"shots": [{"shot_no": 1, "voiceover": "果园现摘，48小时发货",
                         "subtitle": ""}]}
        r = run_tool(_deps(db, ident), "check_risk", {"storyboard": sb})
        assert r.status == "ok", r.error_message
        assert isinstance(r.data["flags"], list)

    def test_check_delivery(self, db):
        org_id, ident = _mk_org(db, role="editor")
        r = run_tool(_deps(db, ident), "check_delivery",
                     {"storyboard": {"shots": []}, "check_type": "plan"})
        assert r.status == "ok"
        assert "delivery" in r.data

    def test_shooting_checklist_validation(self, db):
        org_id, ident = _mk_org(db, role="editor")
        # 非法分镜 → validation 错误而非异常
        r = run_tool(_deps(db, ident), "shooting_checklist",
                     {"storyboard": {"shots": "not-a-list"}})
        assert r.status == "failed"
        assert r.error_code in ("validation", "internal")

    def test_get_task_status_not_found(self, db):
        org_id, ident = _mk_org(db, role="editor")
        r = run_tool(_deps(db, ident), "get_task_status",
                     {"task_id": "no_such_task"})
        assert r.status == "failed" and r.error_code == "not_found"

    def test_get_task_status_ok(self, db):
        org_id, ident = _mk_org(db, role="editor")
        t = models.AITask(org_id=org_id, created_by=ident.user_id,
                          task_type="agent_run", status="running", progress=30)
        db.add(t)
        db.commit()
        r = run_tool(_deps(db, ident), "get_task_status",
                     {"task_id": t.id})
        assert r.status == "ok"
        assert r.data["task"]["task_type"] == "agent_run"

    def test_query_usage(self, db):
        org_id, ident = _mk_org(db, role="editor")
        c = models.AICall(org_id=org_id, task_type="ideas", model="m",
                          total_tokens=100, cost_cents=5)
        db.add(c)
        db.commit()
        r = run_tool(_deps(db, ident), "query_usage", {})
        assert r.status == "ok"
        assert r.data["summary"]["calls"] == 1
        assert r.data["summary"]["total_tokens"] == 100

    def test_regenerate_shot_locked(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        from backend.repositories.storyboard_repository import (
            StoryboardRepository)
        w = WorkRepository(db).create(org_id, ident.user_id, title="t")
        sb_repo = StoryboardRepository(db)
        sb = sb_repo.create(w.id, title="分镜")
        shot = sb_repo.add_shot(sb.id, 1, content={"scene": "果园"})
        sb_repo.set_shot_lock(sb.id, shot.id, True)
        db.commit()
        r = run_tool(_deps(db, ident, ai_generate=lambda p: {}),
                     "regenerate_shot",
                     {"work_id": w.id, "storyboard_id": sb.id,
                      "shot_no": 1, "instruction": "重拍"})
        assert r.status == "failed" and r.error_code == "locked"

    def test_unknown_tool_dispatch(self, db):
        org_id, ident = _mk_org(db, role="editor")
        r = run_tool(_deps(db, ident), "drop_database", {})
        assert r.status == "failed" and r.error_code == "unknown_tool"


# ---------------------------------------------------------------------------
# 5. context：装配 + quota 桩位
# ---------------------------------------------------------------------------

class TestContext:
    def test_assemble_context(self, db):
        org_id, ident = _mk_org(db, role="editor")
        from backend.repositories.work_repository import WorkRepository
        w = WorkRepository(db).create(org_id, ident.user_id, title="ctx",
                                      data={"brief": "b"})
        f = models.Fact(org_id=org_id, content="确认事实",
                        status="confirmed", allow_use=True)
        db.add(f)
        db.commit()
        ctx = assemble_agent_context(db, ident, work_id=w.id)
        assert ctx["user"]["role"] == "editor"
        assert ctx["work"]["id"] == w.id
        assert ctx["work"]["data_keys"] == ["brief"]
        assert len(ctx["facts"]) == 1
        # 跨组织 work_id → work 为 None（不泄露）
        _, ident2 = _mk_org(db, role="editor")
        ctx2 = assemble_agent_context(db, ident2, work_id=w.id)
        assert ctx2["work"] is None

    def test_quota_stub(self):
        q = check_user_quota(user_id="u", org_id="o")
        assert q["enforced"] is False and q["allowed"] is True


# ---------------------------------------------------------------------------
# 6. 迁移 0020：空库全周期 + scratch 拷贝库；真实库只读
# ---------------------------------------------------------------------------

def _alembic_cfg():
    from alembic.config import Config
    return Config(os.path.join(PROJECT_ROOT, "alembic.ini"))


def _tables(engine):
    from sqlalchemy import inspect
    return set(inspect(engine).get_table_names())


class TestMigration0020:
    def test_upgrade_downgrade_upgrade_empty(self, tmp_path, monkeypatch):
        """空库：upgrade head → 表存在；downgrade -1 → 表消失；再 upgrade → 回来。"""
        url = f"sqlite:///{tmp_path}/m.db"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
        reset_engine()
        try:
            from alembic import command
            from sqlalchemy import create_engine
            cfg = _alembic_cfg()
            command.upgrade(cfg, "head")
            eng = create_engine(url)
            try:
                assert {"conversations", "conversation_messages",
                        "agent_steps"} <= _tables(eng)
                # role 约束：非法 role 拒绝
                from sqlalchemy import text
                with eng.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO conversations (id, org_id, user_id) "
                        "VALUES ('c1','o1','u1')"))
                    conn.execute(text(
                        "INSERT INTO conversation_messages "
                        "(id, conversation_id, role) "
                        "VALUES ('m1','c1','user')"))
                    raised = False
                    try:
                        conn.execute(text(
                            "INSERT INTO conversation_messages "
                            "(id, conversation_id, role) "
                            "VALUES ('m2','c1','hacker')"))
                    except Exception:
                        raised = True
                    assert raised, "role CHECK 约束未生效"
            finally:
                eng.dispose()
            command.downgrade(cfg, "0019_ai_call_org")
            eng = create_engine(url)
            try:
                assert not ({"conversations", "conversation_messages",
                             "agent_steps", "idempotency_keys"}
                            & _tables(eng))
                from sqlalchemy import text
                with eng.connect() as conn:
                    ver = conn.execute(
                        text("select version_num from alembic_version")
                    ).fetchone()[0]
                assert ver == "0019_ai_call_org"
            finally:
                eng.dispose()
            command.upgrade(cfg, "head")
            eng = create_engine(url)
            try:
                assert {"conversations", "conversation_messages",
                        "agent_steps", "idempotency_keys"} <= _tables(eng)
            finally:
                eng.dispose()
        finally:
            reset_engine()

    def test_upgrade_on_scratch_copy_of_real_db(self, tmp_path, monkeypatch):
        """scratch 拷贝库可执行迁移；真实库的版本戳与内容哈希保持不变。"""
        assert os.path.exists(REAL_DB), "真实业务库不存在，无法做拷贝验证"

        def _sha256(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        def _version(p):
            import sqlite3
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                return con.execute(
                    "select version_num from alembic_version").fetchone()[0]
            finally:
                con.close()

        before_hash = _sha256(REAL_DB)
        before_ver = _version(REAL_DB)

        scratch = str(tmp_path / "scratch.db")
        shutil.copy(REAL_DB, scratch)  # 只读拷贝
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{scratch}")
        monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
        reset_engine()
        try:
            from alembic import command
            from sqlalchemy import create_engine
            command.upgrade(_alembic_cfg(), "head")
            assert _version(scratch) == "0021_idempotency_keys"
            eng = create_engine(f"sqlite:///{scratch}")
            try:
                assert {"conversations", "conversation_messages",
                        "agent_steps", "idempotency_keys"} <= _tables(eng)
            finally:
                eng.dispose()
        finally:
            reset_engine()

        # 真实库只读：哈希与版本戳不变
        assert _sha256(REAL_DB) == before_hash, "真实库被写入！"
        assert _version(REAL_DB) == before_ver == "0019_ai_call_org"
