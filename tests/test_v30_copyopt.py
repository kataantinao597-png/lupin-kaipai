"""V3.0 D：独立文案优化器测试。

- 文档 CRUD + v1 建档确认
- optimize 生成 draft（mock 网关），原文/已确认版本不受影响
- confirm / restore 只增不改；未确认绝不覆盖
- 段落重写；风险检查触发；失败链路（503 无草稿残留）
- 事实不编造：prompt 级禁令断言
- 权限隔离（跨 org 404 / viewer 不可写）
- 5 种 kind 校验器单元测试
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序

from backend.ai_service import AIConfigError


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/copyopt.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import (
        reset_engine, get_session_factory, get_db,
    )
    from backend.auth.deps import get_current_user
    from backend.security.rate_limit import reset_rate_limit_buckets
    from backend.core.idempotency import reset_idempotency_store
    reset_engine()
    reset_rate_limit_buckets()  # 进程级限流桶是全局的，不清会污染后续测试文件
    reset_idempotency_store()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org1 = Organization(name="合作社A", created_by="u1")
    org2 = Organization(name="合作社B", created_by="u2")
    db.add(org1)
    db.add(org2)
    db.flush()
    for uid in ("u1", "u2", "u_viewer"):
        db.add(AuthUser(id=uid, email=f"{uid}@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org1.id, role="owner"))
    db.add(Membership(user_id="u2", org_id=org2.id, role="owner"))
    db.add(Membership(user_id="u_viewer", org_id=org1.id, role="viewer"))
    db.commit()
    ids = (org1.id, org2.id)
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app
    app.dependency_overrides.clear()

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)

    class Env:
        org1, org2 = ids

        def as_user(self, uid):
            def _u():
                return AuthUser(id=uid, email=f"{uid}@test.local",
                                password_hash="x")
            app.dependency_overrides[get_current_user] = _u
            return client

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_rate_limit_buckets()
    reset_idempotency_store()


class FakeGateway:
    """假网关：返回固定结果；可抛错模拟失败链路。"""

    def __init__(self, fail_with=None):
        self.calls = 0
        self.fail_with = fail_with

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            **kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with("MOCK: AI 不可用")
        data = {
            "text": "改写后的文案正文，实在不端着。",
            "title": "改写后的标题",
            "body": "改写后的正文",
            "hashtags": ["#烟台大樱桃"],
            "cta": "点击购买",
            "changes": ["更口语了"],
            "fact_notes": "无",
        }
        errs, out = validate_fn(data)
        assert not errs, errs
        return out, {"attempts": 1}


@pytest.fixture()
def fake_gw():
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakeGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    return fake


def _doc(client, org_id, kind="optimize", title="测试文案",
         original_text="原文：这苹果真甜。"):
    return client.post("/api/v1/copy/docs",
                       params={"org_id": org_id},
                       json={"title": title, "kind": kind,
                             "original_text": original_text})


class TestDocCRUD:
    def test_create_with_original_builds_v1_confirmed(self, api_env):
        client = api_env.as_user("u1")
        r = _doc(client, api_env.org1)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        assert d["kind"] == "optimize"
        assert d["current_version"]["version_no"] == 1
        assert d["current_version"]["status"] == "confirmed"
        assert d["current_version"]["text"] == "原文：这苹果真甜。"
        assert len(d["versions"]) == 1

    def test_create_invalid_kind_400(self, api_env):
        client = api_env.as_user("u1")
        r = _doc(client, api_env.org1, kind="nope")
        assert r.status_code == 400, r.text

    def test_list_and_get(self, api_env):
        client = api_env.as_user("u1")
        r = _doc(client, api_env.org1, kind="platform", title="发布文案")
        assert r.status_code == 201, r.text
        doc_id = r.json()["data"]["id"]
        r = client.get("/api/v1/copy/docs",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        docs = r.json()["data"]["docs"]
        assert len(docs) == 1 and docs[0]["kind"] == "platform"
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["id"] == doc_id

    def test_delete_doc(self, api_env):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.delete(f"/api/v1/copy/docs/{doc_id}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text

    def test_cross_org_isolation_404(self, api_env):
        c1 = api_env.as_user("u1")
        doc_id = _doc(c1, api_env.org1).json()["data"]["id"]
        c2 = api_env.as_user("u2")
        r = c2.get(f"/api/v1/copy/docs/{doc_id}",
                   params={"org_id": api_env.org2})
        assert r.status_code == 404, r.text

    def test_viewer_cannot_create(self, api_env):
        client = api_env.as_user("u_viewer")
        r = _doc(client, api_env.org1)
        assert r.status_code == 404, r.text  # 防枚举：无权一律 404


class TestOptimizeFlow:
    def test_optimize_creates_draft_original_untouched(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural", "source_text": "原文：这苹果真甜。"})
        assert r.status_code == 200, r.text
        v = r.json()["data"]["version"]
        assert v["version_no"] == 2
        assert v["status"] == "draft"
        assert v["text"] == "改写后的文案正文，实在不端着。"
        assert v["changes_summary"] != ""
        assert "review_reminder" in r.json()["data"]  # 人工复核提示必带
        # 原文 v1 不受影响；当前版本仍是 v1（未确认不覆盖）
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        d = r.json()["data"]
        assert d["current_version"]["version_no"] == 1
        assert d["versions"][0]["text"] == "原文：这苹果真甜。"

    def test_optimize_uses_current_when_no_source(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "concise"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["version"]["source_text"] == "原文：这苹果真甜。"

    def test_optimize_needs_source_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        # generate 类不需要原文；optimize 类无原文无确认版本 → 400
        r = client.post("/api/v1/copy/docs",
                        params={"org_id": api_env.org1},
                        json={"title": "空", "kind": "optimize"})
        doc_id = r.json()["data"]["id"]
        assert r.json()["data"]["current_version"] is None
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural"})
        assert r.status_code == 400, r.text

    def test_optimize_invalid_mode_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "nope", "source_text": "x"})
        assert r.status_code == 400, r.text

    def test_custom_mode_needs_instruction_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "custom", "source_text": "x"})
        assert r.status_code == 400, r.text

    def test_risk_check_runs_and_marks(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural",
                              "source_text": "全国第一甜，不甜不要钱"})
        assert r.status_code == 200, r.text
        v = r.json()["data"]["version"]
        assert v["risk_status"] == "checked"
        assert isinstance(v["risk_flags"], list)

    def test_fact_ids_validated(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural", "source_text": "x",
                              "fact_ids": ["no-such-fact"]})
        assert r.status_code == 404, r.text

    def test_ai_failure_503_no_draft_left(self, api_env):
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        app.dependency_overrides[get_ai_gateway] = lambda: FakeGateway(
            fail_with=AIConfigError)
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural", "source_text": "x"})
        # 诚实失败：503，不伪造内容
        assert r.status_code == 503, r.text
        # 无草稿残留：版本数仍为 1
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        assert len(r.json()["data"]["versions"]) == 1

    def test_platform_kind_structured(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1, kind="platform",
                      original_text="").json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "platform_post", "goal": "卖樱桃"})
        assert r.status_code == 200, r.text
        v = r.json()["data"]["version"]
        st = v["structured"]
        assert st["title"] == "改写后的标题"
        assert st["hashtags"] == ["#烟台大樱桃"]
        assert st["cta"] == "点击购买"


class TestConfirmRestore:
    def _draft(self, client, org_id, fake_gw):
        doc_id = _doc(client, org_id).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": org_id},
                        json={"mode": "natural", "source_text": "原文"})
        return doc_id, r.json()["data"]["version"]["id"]

    def test_confirm_draft(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, vid = self._draft(client, api_env.org1, fake_gw)
        r = client.post(
            f"/api/v1/copy/docs/{doc_id}/versions/{vid}/confirm",
            params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        cur = r.json()["data"]["current_version"]
        assert cur["version_no"] == 2 and cur["status"] == "confirmed"
        # v1 历史保留且仍是 confirmed（只增不改）
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        vs = r.json()["data"]["versions"]
        assert [v["status"] for v in vs] == ["confirmed", "confirmed"]

    def test_confirm_non_draft_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        v1 = client.get(f"/api/v1/copy/docs/{doc_id}",
                        params={"org_id": api_env.org1}
                        ).json()["data"]["versions"][0]["id"]
        r = client.post(
            f"/api/v1/copy/docs/{doc_id}/versions/{v1}/confirm",
            params={"org_id": api_env.org1})
        assert r.status_code == 400, r.text

    def test_restore_creates_new_confirmed_row(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, vid = self._draft(client, api_env.org1, fake_gw)
        client.post(f"/api/v1/copy/docs/{doc_id}/versions/{vid}/confirm",
                    params={"org_id": api_env.org1})
        v1 = client.get(f"/api/v1/copy/docs/{doc_id}",
                        params={"org_id": api_env.org1}
                        ).json()["data"]["versions"][0]
        r = client.post(
            f"/api/v1/copy/docs/{doc_id}/versions/{v1['id']}/restore",
            params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        v3 = r.json()["data"]["version"]
        assert v3["version_no"] == 3
        assert v3["status"] == "confirmed"
        assert v3["text"] == v1["text"]  # 内容回到 v1
        assert v3["restored_from"] == v1["id"]
        # 旧行一个没动：共 3 行
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        assert len(r.json()["data"]["versions"]) == 3


class TestRewriteParagraph:
    def test_paragraph_rewrite_creates_draft(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(
            client, api_env.org1,
            original_text="第一段。\n\n第二段。\n\n第三段。").json()["data"]["id"]
        v1 = client.get(f"/api/v1/copy/docs/{doc_id}",
                        params={"org_id": api_env.org1}
                        ).json()["data"]["versions"][0]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/rewrite-paragraph",
                        params={"org_id": api_env.org1},
                        json={"version_id": v1, "paragraph_index": 1,
                              "instruction": "更口语"})
        assert r.status_code == 200, r.text
        v = r.json()["data"]["version"]
        assert v["status"] == "draft"
        assert v["source_text"] == "第一段。\n\n第二段。\n\n第三段。"
        assert v["ai_meta"]["paragraph_index"] == 1

    def test_paragraph_index_out_of_range_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1,
                      original_text="只有一段").json()["data"]["id"]
        v1 = client.get(f"/api/v1/copy/docs/{doc_id}",
                        params={"org_id": api_env.org1}
                        ).json()["data"]["versions"][0]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/rewrite-paragraph",
                        params={"org_id": api_env.org1},
                        json={"version_id": v1, "paragraph_index": 5})
        assert r.status_code == 400, r.text


class TestDropDraft:
    def test_drop_draft_ok(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural", "source_text": "原文"})
        vid = r.json()["data"]["version"]["id"]
        r = client.delete(
            f"/api/v1/copy/docs/{doc_id}/versions/{vid}",
            params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/v1/copy/docs/{doc_id}",
                       params={"org_id": api_env.org1})
        assert len(r.json()["data"]["versions"]) == 1  # 只剩 v1

    def test_drop_confirmed_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id = _doc(client, api_env.org1).json()["data"]["id"]
        v1 = client.get(f"/api/v1/copy/docs/{doc_id}",
                        params={"org_id": api_env.org1}
                        ).json()["data"]["versions"][0]["id"]
        r = client.delete(
            f"/api/v1/copy/docs/{doc_id}/versions/{v1}",
            params={"org_id": api_env.org1})
        assert r.status_code == 400, r.text  # 已确认版本不可删


class TestPromptDiscipline:
    """事实不编造：prompt 级禁令必须存在（5 种 kind × 任意 mode）。"""

    def test_fact_ban_in_prompt(self):
        from backend.ai.prompts.copyopt_v1 import (
            build_copyopt_messages, FACT_RULES)
        for w in ["产品功效", "产地认证", "销量", "排名", "产品规格",
                  "质量等级", "用户评价", "价格", "优惠", "服务承诺"]:
            assert w in FACT_RULES, w
        for kind in ["optimize", "generate", "script", "platform", "adapt"]:
            c = build_copyopt_messages(kind, "ecommerce",
                                       source_text="原文",
                                       goal="卖货")[0]["content"]
            for w in ["销量", "服务承诺", "产品功效", "用户评价"]:
                assert w in c, (kind, w)
            # 不改变事实含义
            assert "不改变事实含义" in c

    def test_no_default_literary_style(self):
        from backend.ai.prompts.copyopt_v1 import build_copyopt_messages
        c = build_copyopt_messages("optimize", "natural",
                                   source_text="原文")[0]["content"]
        assert "自然口语化" in c
        # 默认 natural 不应出现文艺腔引导
        assert "华丽" not in c and "文艺" not in c

    def test_prompt_version_registered(self):
        from backend.ai.prompts import get_prompt_version
        assert get_prompt_version("copy_optimize") == "copyopt_v1"

    def test_five_kind_validators(self):
        from backend.api.v1.copyopt import _validate_copyopt
        good = {"text": "正文", "changes": ["改了"],
                "fact_notes": "无"}
        for kind in ["optimize", "generate", "script", "adapt"]:
            errs, norm = _validate_copyopt(kind, dict(good))
            assert errs == [], (kind, errs)
            assert norm["text"] == "正文"
        errs, norm = _validate_copyopt("platform", {
            "title": "题", "body": "文", "hashtags": ["#x"],
            "cta": "买", "changes": [], "fact_notes": ""})
        assert errs == []
        assert norm["hashtags"] == ["#x"]
        # 缺字段 → 报错
        errs, _ = _validate_copyopt("optimize", {"changes": []})
        assert errs
        errs, _ = _validate_copyopt("platform", {"title": "题"})
        assert errs
