"""V3.0 B-4/B-5：产品关联生成 + 版本过期标记测试。

B-4：ideas/storyboard 传 product_id 时，后端自动把该产品的已确认事实
     并入 prompt grounding（只看 confirmed+allow_use+未过期；未确认一律不带入）。
B-5：产品资料 PATCH 后，引用旧版本的作品只打 stale 标记，绝不改写历史内容。
"""
import os
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """三组织环境：org1(owner u1 + viewer u_viewer)、org2(owner u2)。"""
    url = f"sqlite:///{tmp_path}/b4b5.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import (
        reset_engine, get_session_factory, get_db,
    )
    from backend.auth.deps import get_current_user
    reset_engine()
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

        def db(self):
            return get_session_factory()()

    yield Env()


def _create_product(client, org_id, code="P-001", name="烟台大樱桃"):
    r = client.post("/api/v1/products", json={
        "org_id": org_id, "code": code, "name": name,
        "category": "水果", "origin": "山东烟台",
        "brand_profile": "果园直供", "brand_tone": "实在",
        "selling_points": ["脆甜多汁"],
        "ad_restrictions": "不夸大功效", "no_expand_claims": "不编造认证",
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _confirmed_fact(env, org_id, product_id, content,
                    allow_use=True, expired=False):
    """直接走 repository 建事实并流转到 confirmed（测试脚手架）。"""
    from datetime import datetime, timedelta, timezone
    from backend.models import Fact
    from backend.repositories.fact_repository import FactRepository
    db = env.db()
    try:
        repo = FactRepository(db)
        exp = (datetime.now(timezone.utc) - timedelta(days=1)) if expired else None
        f = repo.create(org_id, content, product_id=product_id,
                        source_type="official_document", source_ref="检测报告",
                        expires_at=exp)
        repo.transition(f.id, org_id, "pending_review",
                        operator_id="u1", reason="提交审核")
        f = repo.transition(f.id, org_id, "confirmed",
                            operator_id="u1", reason="核验通过",
                            allow_use=True)
        if not allow_use:
            # 模拟"确认后被关闭使用许可"（状态机禁止确认时直接设 False）
            db.query(Fact).filter(Fact.id == f.id).update(
                {"allow_use": False})
        db.commit()
        return {"id": f.id, "content": f.content}
    finally:
        db.close()


def _draft_fact(env, org_id, product_id, content):
    from backend.repositories.fact_repository import FactRepository
    db = env.db()
    try:
        repo = FactRepository(db)
        f = repo.create(org_id, content, product_id=product_id)
        db.commit()
        return {"id": f.id}
    finally:
        db.close()


def _create_work(client, org_id, product_ref=None):
    data = {}
    if product_ref is not None:
        data["product_ref"] = product_ref
    r = client.post("/api/v1/works", json={
        "org_id": org_id, "title": "测试作品", "data": data})
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---------- B-4：产品事实 grounding ----------

class TestProductGrounding:
    def test_only_confirmed_usable_facts(self, api_env):
        """只带入 confirmed+allow_use+未过期的；草稿/禁用/过期/别家产品一律排除。"""
        from backend.api.v1.products import get_product_grounding
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        good = _confirmed_fact(api_env, api_env.org1, p["id"], "樱桃产自烟台福山")
        _draft_fact(api_env, api_env.org1, p["id"], "草稿：樱桃很甜")
        _confirmed_fact(api_env, api_env.org1, p["id"], "禁用事实", allow_use=False)
        _confirmed_fact(api_env, api_env.org1, p["id"], "过期事实", expired=True)
        p2 = _create_product(client, api_env.org1, code="P-002", name="德州扒鸡")
        _confirmed_fact(api_env, api_env.org1, p2["id"], "扒鸡产自德州")

        db = api_env.db()
        try:
            prod, facts = get_product_grounding(db, api_env.org1, p["id"])
        finally:
            db.close()
        assert prod["id"] == p["id"] and prod["version"] == 1
        contents = [f["content"] for f in facts]
        assert contents == ["樱桃产自烟台福山"], contents
        assert all(f["id"] for f in facts)
        assert good["id"] in [f["id"] for f in facts]

    def test_fact_referenced_by_code(self, api_env):
        """fact.product_id 用产品 code 引用也能命中（B-2 自由字符串兼容）。"""
        from backend.api.v1.products import get_product_grounding
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1, code="CHERRY-26")
        _confirmed_fact(api_env, api_env.org1, "CHERRY-26", "code 引用的事实")
        db = api_env.db()
        try:
            _, facts = get_product_grounding(db, api_env.org1, p["id"])
        finally:
            db.close()
        assert [f["content"] for f in facts] == ["code 引用的事实"]

    def test_cross_org_product_404(self, api_env):
        """跨组织拿产品 → 404（跨产品隔离）。"""
        from backend.api.v1.products import get_product_grounding
        from backend.core.exceptions import BusinessError
        client = api_env.as_user("u2")
        p = _create_product(client, api_env.org2, code="O2-P")
        db = api_env.db()
        try:
            with pytest.raises(BusinessError) as ei:
                get_product_grounding(db, api_env.org1, p["id"])
        finally:
            db.close()
        assert ei.value.http_status == 404

    def test_merge_dedup_client_first(self, api_env):
        """客户端已传的事实不重复；客户端优先。"""
        from backend.api.v1.products import merge_grounding_facts
        client_facts = [{"id": "f1", "content": "客户端版"}]
        product_facts = [
            {"id": "f1", "content": "产品版（应被去重）"},
            {"id": "f2", "content": "产品独有"},
        ]
        merged = merge_grounding_facts(client_facts, product_facts)
        assert [f["id"] for f in merged] == ["f1", "f2"]
        assert merged[0]["content"] == "客户端版"

    def test_resolve_without_product_id_unchanged(self, api_env):
        """无 product_id：行为与之前完全一致（原样返回客户端 facts）。"""
        from backend.api.v1.ideas import _resolve_product_facts
        req = SimpleNamespace(product_id=None, org_id=None,
                              facts=[{"content": "a"}])
        prod, facts = _resolve_product_facts(req)
        assert prod is None
        assert facts == [{"content": "a"}]

    def test_resolve_with_product_id_feeds_prompt(self, api_env):
        """有 product_id：prompt 里出现已确认事实文本；未确认的不出现。"""
        from backend.api.v1.ideas import _resolve_product_facts
        from backend.ai.prompts.ideas_v1 import build_idea_messages
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        _confirmed_fact(api_env, api_env.org1, p["id"], "确认：果径28mm以上")
        _draft_fact(api_env, api_env.org1, p["id"], "草稿：甜过初恋")
        req = SimpleNamespace(product_id=p["id"], org_id=api_env.org1,
                              facts=[{"content": "客户端事实",
                                      "status": "confirmed", "allow_use": True}])
        prod, facts = _resolve_product_facts(req)
        assert prod["id"] == p["id"]
        msgs = build_idea_messages({"product_name": "樱桃"}, facts)
        text = msgs[0]["content"]
        assert "确认：果径28mm以上" in text
        assert "客户端事实" in text
        assert "草稿：甜过初恋" not in text

    def test_resolve_bad_product_404(self, api_env):
        from backend.api.v1.ideas import _resolve_product_facts
        from backend.core.exceptions import BusinessError
        req = SimpleNamespace(product_id="nope", org_id=api_env.org1, facts=[])
        with pytest.raises(BusinessError) as ei:
            _resolve_product_facts(req)
        assert ei.value.http_status == 404

    def test_async_runner_resolves_product_facts(self, api_env):
        """异步 runner：payload 带 product_id 时同样解析已确认事实。"""
        from backend.ai_tasks.runners import _resolve_payload_product_facts
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        _confirmed_fact(api_env, api_env.org1, p["id"], "异步：已确认")
        payload = {"product_id": p["id"], "_org_id": api_env.org1,
                   "facts": [{"content": "客户端"}]}
        facts, prod = _resolve_payload_product_facts(payload, payload["facts"])
        assert prod["id"] == p["id"]
        assert "异步：已确认" in [f["content"] for f in facts]

    def test_async_runner_no_product_passthrough(self, api_env):
        from backend.ai_tasks.runners import _resolve_payload_product_facts
        facts, prod = _resolve_payload_product_facts(
            {"facts": [{"content": "x"}]}, [{"content": "x"}])
        assert prod is None and facts == [{"content": "x"}]


# ---------- B-5：版本过期标记 ----------

class TestProductRefStale:
    def _patch(self, client, pid, org_id, version, **fields):
        body = {"org_id": org_id, "expected_version": version}
        body.update(fields)
        r = client.patch(f"/api/v1/products/{pid}", json=body)
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def test_patch_marks_stale_and_counts(self, api_env):
        """产品 PATCH 后：引用旧版本的标记为 stale，响应带计数。"""
        from backend.repositories.product_repository import ProductRepository
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        w = _create_work(client, api_env.org1, {
            "product_id": p["id"], "product_version": 1,
            "product_code": p["code"], "product_name": p["name"]})
        out = self._patch(client, p["id"], api_env.org1, 1,
                          brand_profile="新版品牌故事")
        assert out["version"] == 2
        assert out["stale_refs_marked"] == 1
        db = api_env.db()
        try:
            assert ProductRepository(db).is_ref_stale("work", w["id"]) is True
        finally:
            db.close()

    def test_old_content_not_rewritten(self, api_env):
        """标记前后作品内容一字不改（铁律）。"""
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        w = _create_work(client, api_env.org1, {
            "product_id": p["id"], "product_version": 1,
            "product_code": p["code"], "product_name": p["name"]})
        before = w["data"]
        self._patch(client, p["id"], api_env.org1, 1, selling_points=["新卖点"])
        r = client.get(f"/api/v1/works/{w['id']}")
        assert r.status_code == 200, r.text
        after = r.json()["data"]["data"]
        assert after == before

    def test_resave_preserves_stale(self, api_env):
        """只保存不重新生成：stale 标记不被洗掉。"""
        from backend.repositories.product_repository import ProductRepository
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        w = _create_work(client, api_env.org1, {
            "product_id": p["id"], "product_version": 1,
            "product_code": p["code"], "product_name": p["name"]})
        self._patch(client, p["id"], api_env.org1, 1, origin="新产地")
        # 模拟前端普通保存（引用仍是旧版本）
        r = client.patch(f"/api/v1/works/{w['id']}", json={
            "data": w["data"], "version": w["version"]})
        assert r.status_code == 200, r.text
        db = api_env.db()
        try:
            assert ProductRepository(db).is_ref_stale("work", w["id"]) is True
        finally:
            db.close()

    def test_regen_with_new_version_clears_stale(self, api_env):
        """用新版本重新生成后保存：stale 解除。"""
        from backend.repositories.product_repository import ProductRepository
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        w = _create_work(client, api_env.org1, {
            "product_id": p["id"], "product_version": 1,
            "product_code": p["code"], "product_name": p["name"]})
        out = self._patch(client, p["id"], api_env.org1, 1, origin="新产地")
        assert out["stale_refs_marked"] == 1
        data = dict(w["data"])
        data["product_ref"] = {
            "product_id": p["id"], "product_version": out["version"],
            "product_code": p["code"], "product_name": p["name"]}
        r = client.patch(f"/api/v1/works/{w['id']}", json={
            "data": data, "version": w["version"]})
        assert r.status_code == 200, r.text
        db = api_env.db()
        try:
            assert ProductRepository(db).is_ref_stale("work", w["id"]) is False
        finally:
            db.close()

    def test_get_work_returns_stale_flag(self, api_env):
        """作品详情返回 product_ref_stale=True。"""
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        w = _create_work(client, api_env.org1, {
            "product_id": p["id"], "product_version": 1,
            "product_code": p["code"], "product_name": p["name"]})
        self._patch(client, p["id"], api_env.org1, 1, name="新名称")
        r = client.get(f"/api/v1/works/{w['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["product_ref_stale"] is True

    def test_save_without_ref_no_rows(self, api_env):
        """没关联产品的作品：不产生引用记录。"""
        from sqlalchemy import select
        from backend.models import ProductRef
        from backend.repositories.product_repository import ProductRepository
        client = api_env.as_user("u1")
        w = _create_work(client, api_env.org1)
        db = api_env.db()
        try:
            rows = list(db.scalars(
                select(ProductRef).where(ProductRef.ref_id == w["id"])))
            assert rows == []
            assert ProductRepository(db).is_ref_stale("work", w["id"]) is False
        finally:
            db.close()

    def test_cross_org_product_ref_ignored(self, api_env):
        """拿别组织的产品 id 存引用 → 引用不建立（组织隔离）。"""
        from sqlalchemy import func, select
        from backend.models import ProductRef
        from backend.repositories.product_repository import ProductRepository
        client2 = api_env.as_user("u2")
        p2 = _create_product(client2, api_env.org2, code="O2-P")
        client = api_env.as_user("u1")
        w = _create_work(client, api_env.org1, {
            "product_id": p2["id"], "product_version": 1,
            "product_code": p2["code"], "product_name": p2["name"]})
        db = api_env.db()
        try:
            assert ProductRepository(db).is_ref_stale("work", w["id"]) is False
            n = db.scalar(
                select(func.count()).select_from(ProductRef)
                .where(ProductRef.ref_id == w["id"]))
            assert n == 0
        finally:
            db.close()

    def test_patch_without_refs_zero_count(self, api_env):
        """没有引用时 PATCH：stale_refs_marked=0，不报错。"""
        client = api_env.as_user("u1")
        p = _create_product(client, api_env.org1)
        out = self._patch(client, p["id"], api_env.org1, 1, origin="x")
        assert out["stale_refs_marked"] == 0
