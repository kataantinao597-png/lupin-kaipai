"""V3.0 E：多平台内容适配测试。

- 平台规则配置可读（含免责声明）
- 从已确认文案版本生成平台版本（AI mock / 手动）
- 源版本必须已确认；不支持的平台 400
- 各平台版本独立保存，互不覆盖；revision_no 递增
- 审核状态流转；导出内容正确；权限隔离
- AI 失败 503 且不留脏记录；prompt 级事实禁令与免责声明断言
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序

from backend.ai_service import AIFatalError


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/platform.db"
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
    reset_rate_limit_buckets()
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


class FakePlatformGateway:
    """假网关：返回平台改写结果；可抛错模拟失败链路。"""

    def __init__(self, fail_with=None):
        self.calls = 0
        self.fail_with = fail_with

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            **kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with("MOCK: AI 不可用")
        data = {
            "text": "AI 改的正文，实在不端着。",
            "title": "AI 改的抖音标题",
            "body": "AI 改的抖音正文，实在不端着。",
            "hashtags": ["#烟台大樱桃", "山东好品"],
            "cover_text": "果大汁多",
            "pinned_comment": "想看切果的扣 1",
            "cta": "点击购买",
            "changes": ["按抖音调性改了开头"],
            "fact_notes": "无新增事实",
        }
        errs, out = validate_fn(data)
        assert not errs, errs
        return out, {"attempts": 1}


@pytest.fixture()
def fake_gw():
    from backend.app import app
    from backend.core.deps import get_ai_gateway
    fake = FakePlatformGateway()
    app.dependency_overrides[get_ai_gateway] = lambda: fake
    return fake


def _doc_with_confirmed(client, org_id):
    """建文档（带原文 → v1 即确认版本），返回 (doc_id, version_id)。"""
    r = client.post("/api/v1/copy/docs", params={"org_id": org_id},
                    json={"title": "测试文案", "kind": "optimize",
                          "original_text": "原文：这樱桃真甜，果大汁多。"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["data"]["id"]
    r = client.get(f"/api/v1/copy/docs/{doc_id}", params={"org_id": org_id})
    assert r.status_code == 200, r.text
    cur = r.json()["data"]["current_version"]
    assert cur["status"] == "confirmed"
    return doc_id, cur["id"]


def _make(client, org_id, doc_id, version_id, platform="douyin", use_ai=True,
          **kw):
    body = {"copy_version_id": version_id, "platform": platform,
            "use_ai": use_ai}
    body.update(kw)
    return client.post(f"/api/v1/platforms/copy/docs/{doc_id}/platforms",
                       params={"org_id": org_id}, json=body)


class TestPlatformRules:
    def test_rules_readable_with_disclaimer(self, api_env):
        client = api_env.as_user("u1")
        r = client.get("/api/v1/platforms/rules",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert set(data["platforms"].keys()) == {
            "douyin", "kuaishou", "xiaohongshu", "shipinhao"}
        assert "仅供参考" in data["disclaimer"]
        # 规则是建议口径，不写绝对化表述
        import json as _json
        dumped = _json.dumps(data, ensure_ascii=False)
        assert "保证曝光" not in dumped and "流量公式" not in dumped


class TestCreatePlatformVersion:
    def test_ai_create_from_confirmed(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = _make(client, api_env.org1, doc_id, ver_id, platform="douyin")
        assert r.status_code == 201, r.text
        pv = r.json()["data"]["platform_version"]
        assert pv["platform"] == "douyin"
        assert pv["source_version_no"] == 1
        assert pv["copy_version_id"] == ver_id
        assert pv["title"] == "AI 改的抖音标题"
        assert pv["hashtags"] == ["烟台大樱桃", "山东好品"]  # # 已剥离
        assert pv["review_status"] == "unreviewed"
        assert pv["revision_no"] == 1
        assert "人工确认" in r.json()["data"]["review_reminder"]

    def test_manual_create(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = _make(client, api_env.org1, doc_id, ver_id,
                  platform="xiaohongshu", use_ai=False,
                  title="手填标题", body="手填正文",
                  hashtags=["#种草"], cta="收藏")
        assert r.status_code == 201, r.text
        pv = r.json()["data"]["platform_version"]
        assert pv["title"] == "手填标题"
        assert pv["hashtags"] == ["种草"]
        assert pv["ai_meta"]["mode"] == "manual"

    def test_manual_needs_title_body_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = _make(client, api_env.org1, doc_id, ver_id,
                  platform="douyin", use_ai=False, title="", body="")
        assert r.status_code == 400, r.text

    def test_manual_works_without_ai_configured(self, api_env):
        """手动模式不经过 AI 网关：无 key 也能保存（回归：Depends 懒加载）。"""
        from backend.core.deps import get_ai_gateway
        from backend.app import app
        # 确保网关依赖未被 mock，且当前环境无 AI key
        app.dependency_overrides.pop(get_ai_gateway, None)
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = client.post(f"/api/v1/platforms/copy/docs/{doc_id}/platforms",
                        params={"org_id": api_env.org1},
                        json={"copy_version_id": ver_id, "platform": "douyin",
                              "use_ai": False, "title": "无AI标题", "body": "无AI正文"})
        assert r.status_code == 201, r.text
        assert r.json()["data"]["platform_version"]["ai_meta"]["mode"] == "manual"

    def test_source_must_be_confirmed_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        # 建一个 draft 版本：先建文档再 optimize（不 confirm）
        r = client.post("/api/v1/copy/docs", params={"org_id": api_env.org1},
                        json={"title": "草稿文案", "kind": "optimize"})
        doc_id = r.json()["data"]["id"]
        from backend.core.deps import get_ai_gateway
        from backend.app import app
        app.dependency_overrides[get_ai_gateway] = lambda: fake_gw
        r = client.post(f"/api/v1/copy/docs/{doc_id}/optimize",
                        params={"org_id": api_env.org1},
                        json={"mode": "natural", "source_text": "草稿原文"})
        assert r.status_code == 200, r.text
        draft_id = r.json()["data"]["version"]["id"]
        r = _make(client, api_env.org1, doc_id, draft_id, platform="douyin")
        assert r.status_code == 400, r.text
        assert "已确认" in r.text

    def test_unsupported_platform_400(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = _make(client, api_env.org1, doc_id, ver_id, platform="weibo")
        assert r.status_code == 400, r.text

    def test_revision_increments_per_platform(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r1 = _make(client, api_env.org1, doc_id, ver_id, platform="douyin")
        r2 = _make(client, api_env.org1, doc_id, ver_id, platform="douyin")
        assert r1.json()["data"]["platform_version"]["revision_no"] == 1
        assert r2.json()["data"]["platform_version"]["revision_no"] == 2

    def test_ai_failure_503_no_dirty_record(self, api_env):
        from backend.app import app
        from backend.core.deps import get_ai_gateway
        app.dependency_overrides[get_ai_gateway] = \
            lambda: FakePlatformGateway(fail_with=AIFatalError)
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        r = _make(client, api_env.org1, doc_id, ver_id, platform="douyin")
        assert r.status_code == 503, r.text
        r = client.get(f"/api/v1/platforms/copy/docs/{doc_id}/platforms",
                       params={"org_id": api_env.org1})
        assert r.json()["data"]["items"] == []


class TestVersionIndependence:
    def test_edit_one_platform_does_not_touch_other(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        dy = _make(client, api_env.org1, doc_id, ver_id,
                   platform="douyin").json()["data"]["platform_version"]
        xhs = _make(client, api_env.org1, doc_id, ver_id,
                    platform="xiaohongshu").json()["data"]["platform_version"]
        r = client.patch(f"/api/v1/platforms/{dy['id']}",
                         params={"org_id": api_env.org1},
                         json={"title": "只改抖音的标题",
                               "pinned_comment": "只改抖音的置顶"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["platform_version"]["title"] == "只改抖音的标题"
        r = client.get(f"/api/v1/platforms/{xhs['id']}",
                       params={"org_id": api_env.org1})
        other = r.json()["data"]["platform_version"]
        assert other["title"] == "AI 改的抖音标题"  # 未被覆盖
        assert other["pinned_comment"] == "想看切果的扣 1"

    def test_list_grouped_and_disclaimer(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        _make(client, api_env.org1, doc_id, ver_id, platform="douyin")
        _make(client, api_env.org1, doc_id, ver_id, platform="kuaishou")
        r = client.get(f"/api/v1/platforms/copy/docs/{doc_id}/platforms",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert len(data["items"]) == 2
        assert "仅供参考" in data["disclaimer"]


class TestReviewAndExport:
    def test_review_flow(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        pv = _make(client, api_env.org1, doc_id, ver_id,
                   platform="shipinhao").json()["data"]["platform_version"]
        assert pv["review_status"] == "unreviewed"
        r = client.post(f"/api/v1/platforms/{pv['id']}/review",
                        params={"org_id": api_env.org1},
                        json={"reviewed": True})
        assert r.json()["data"]["platform_version"]["review_status"] == "reviewed"
        r = client.post(f"/api/v1/platforms/{pv['id']}/review",
                        params={"org_id": api_env.org1},
                        json={"reviewed": False})
        assert r.json()["data"]["platform_version"]["review_status"] == "unreviewed"

    def test_export_content(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        pv = _make(client, api_env.org1, doc_id, ver_id,
                   platform="douyin").json()["data"]["platform_version"]
        r = client.get(f"/api/v1/platforms/{pv['id']}/export",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["platform"] == "douyin"
        assert data["platform_name"] == "抖音"
        assert "AI 改的抖音标题" in data["text"]
        assert "#烟台大樱桃" in data["text"]
        assert "仅供参考" in data["text"]

    def test_delete(self, api_env, fake_gw):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        pv = _make(client, api_env.org1, doc_id, ver_id,
                   platform="douyin").json()["data"]["platform_version"]
        r = client.delete(f"/api/v1/platforms/{pv['id']}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/v1/platforms/{pv['id']}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text


class TestPermissions:
    def test_cross_org_isolation_404(self, api_env, fake_gw):
        c1 = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(c1, api_env.org1)
        pv = _make(c1, api_env.org1, doc_id, ver_id,
                   platform="douyin").json()["data"]["platform_version"]
        c2 = api_env.as_user("u2")
        r = c2.get(f"/api/v1/platforms/{pv['id']}",
                   params={"org_id": api_env.org2})
        assert r.status_code == 404, r.text

    def test_viewer_cannot_create(self, api_env, fake_gw):
        client = api_env.as_user("u_viewer")
        r = client.post("/api/v1/platforms/copy/docs/x/platforms",
                        params={"org_id": api_env.org1},
                        json={"copy_version_id": "y", "platform": "douyin"})
        assert r.status_code == 404, r.text  # 防枚举：无权一律 404


class TestPromptDiscipline:
    def test_prompt_has_fact_ban_and_disclaimer(self):
        from backend.ai.prompts import build_platform_messages
        msgs = build_platform_messages(
            "源文案", {"title": "t", "body": "b", "hashtags": [], "cta": ""},
            "douyin", {"name": "抖音", "tone_guide": "快",
                       "title": {}, "body": {}, "hashtags": {},
                       "cover_text": {}, "cta_style": ""},
            facts=[], product_name="", no_expand_claims="")
        text = msgs[0]["content"]
        assert "绝不添加" in text  # 事实禁令
        assert "不要编造任何流量公式" in text  # 免责声明
        assert "建议参考" in text

    def test_validate_platform_result(self):
        from backend.ai.prompts import validate_platform_result
        errs, out = validate_platform_result({"title": "", "body": "b"})
        assert errs  # 缺 title
        errs, out = validate_platform_result({
            "title": "t", "body": "b", "hashtags": ["#a", "b"],
            "changes": ["x"]})
        assert not errs, errs
        assert out["hashtags"] == ["a", "b"]
        assert out["cover_text"] == ""
