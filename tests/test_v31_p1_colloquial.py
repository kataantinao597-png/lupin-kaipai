"""V3.1 P1：拉呱版改写升级回归测试。

- fixture 样本：v1/v2 同输入可重复执行，断言约束齐备
- v2 事实 grounding：已确认事实注入 prompt；draft 状态事实不注入
- registry：colloquial_rewrite → colloquial_v2
- API：prompt_version/facts_used 返回；无组织头时 facts_used=0 优雅降级
"""
import json
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序

FIXTURE = os.path.join(PROJECT_ROOT, "tests", "fixtures",
                        "colloquial_v1_v2.json")


def _samples():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["samples"]


class TestFixtureSamples:
    def test_fixture_loads(self):
        samples = _samples()
        assert len(samples) == 3
        assert all("text" in s and "facts" in s for s in samples)

    def test_v1_v2_both_build(self):
        from backend.ai.prompts.colloquial_v1 import (
            build_colloquial_messages as build_v1)
        from backend.ai.prompts.colloquial_v2 import (
            build_colloquial_messages as build_v2)
        for s in _samples():
            m1 = build_v1(s["text"], s["product_name"])
            m2 = build_v2(s["text"], s["product_name"], s["facts"])
            assert len(m1) == 1 and len(m2) == 1
            # 共同约束：口语化、不改变原意、只输出 JSON
            for content in (m1[0]["content"], m2[0]["content"]):
                assert "口语" in content
                assert "原意" in content
                assert "只输出 JSON" in content
                assert s["text"] in content

    def test_v2_injects_confirmed_facts(self):
        from backend.ai.prompts.colloquial_v2 import build_colloquial_messages
        s = _samples()[0]  # s1：两条 confirmed 事实
        content = build_colloquial_messages(
            s["text"], s["product_name"], s["facts"])[0]["content"]
        assert "山东烟台" in content
        assert "果大核小" in content
        assert "事实保真" in content

    def test_v2_skips_draft_facts(self):
        # s3：draft 状态事实不得注入（防未确认信息污染改写）
        from backend.ai.prompts.colloquial_v2 import build_colloquial_messages
        s = _samples()[2]
        content = build_colloquial_messages(
            s["text"], s["product_name"], s["facts"])[0]["content"]
        assert "销量全国第一" not in content

    def test_v2_forbids_fabrication(self):
        from backend.ai.prompts.colloquial_v2 import build_colloquial_messages
        content = build_colloquial_messages("测试文案")[0]["content"]
        for kw in ("功效", "销量", "认证", "价格", "优惠"):
            assert kw in content  # 禁止新增这些宣传性说法必须写进提示词

    def test_registry_points_to_v2(self):
        from backend.ai.prompts import get_prompt_version, get_prompt_module
        assert get_prompt_version("colloquial_rewrite") == "colloquial_v2"
        mod = get_prompt_module("colloquial_rewrite")
        assert mod.VERSION == "colloquial_v2"
        # v1 文件保留（版本规则）
        from backend.ai.prompts import colloquial_v1
        assert colloquial_v1.VERSION == "colloquial_v1"


class _FakeGateway:
    """网关替身：捕获 messages，返回固定改写结果。"""

    def __init__(self):
        self.last_messages = None

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            model=None, temperature=0.3, max_attempts=4,
                            prompt_version=None):
        self.last_messages = make_messages(None)
        data = {"rewritten": "改写后的口语", "changes": ["改了语气"]}
        errs, out = validate_fn(data)
        assert not errs
        return out, {"attempts": 1}


@pytest.fixture()
def colloq_env(tmp_path, monkeypatch):
    url = "sqlite:///%s/v31p1cq.db" % tmp_path
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.core.idempotency import reset_idempotency_store
    from backend.security.rate_limit import reset_rate_limit_buckets
    from backend.core import deps as core_deps
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()
    from alembic.config import Config
    from alembic import command
    command.upgrade(Config(os.path.join(PROJECT_ROOT, "alembic.ini")), "head")

    from backend.models import Organization, Membership, Fact
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="合作社C", created_by="u1")
    db.add(org)
    db.flush()
    db.add(AuthUser(id="u1", email="u1@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org.id, role="owner"))
    # 一条 confirmed 可用事实 + 一条 draft（不得注入）
    db.add(Fact(org_id=org.id, product_id="p1", content="产地：山东烟台",
                category="产地", status="confirmed", allow_use=True))
    db.add(Fact(org_id=org.id, product_id="p1", content="销量全国第一",
                category="销量", status="draft", allow_use=True))
    db.commit()
    oid = org.id
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
    fake = _FakeGateway()
    app.dependency_overrides[core_deps.get_ai_gateway] = lambda: fake
    client = TestClient(app, raise_server_exceptions=False)

    def _u():
        return AuthUser(id="u1", email="u1@test.local", password_hash="x")
    app.dependency_overrides[get_current_user] = _u

    class Env:
        pass
    Env.org1 = oid
    Env.gw = fake
    Env.client = client

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()


class TestColloquialAPIFacts:
    def test_prompt_version_and_facts_used(self, colloq_env):
        r = colloq_env.client.post(
            "/api/colloquial-rewrite",
            json={"text": "这款苹果口感脆甜，欢迎购买。",
                  "product_name": "烟台苹果", "product_id": "p1"},
            headers={"X-Org-Id": colloq_env.org1,
                     "Idempotency-Key": "cq-k1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["prompt_version"] == "colloquial_v2"
        assert body["facts_used"] == 1  # 只有 confirmed 那条
        # prompt 里确实注入了已确认事实，且 draft 的没进去
        prompt = colloq_env.gw.last_messages[0]["content"]
        assert "产地：山东烟台" in prompt
        assert "销量全国第一" not in prompt

    def test_no_org_header_graceful(self, colloq_env):
        r = colloq_env.client.post(
            "/api/colloquial-rewrite",
            json={"text": "这款苹果口感脆甜，欢迎购买。"},
            headers={"Idempotency-Key": "cq-k2"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["facts_used"] == 0
        assert body["prompt_version"] == "colloquial_v2"

    def test_foreign_org_header_404(self, colloq_env):
        r = colloq_env.client.post(
            "/api/colloquial-rewrite",
            json={"text": "这款苹果口感脆甜，欢迎购买。"},
            headers={"X-Org-Id": "not-exist-org",
                     "Idempotency-Key": "cq-k3"})
        assert r.status_code == 404
