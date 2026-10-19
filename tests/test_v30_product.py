"""V3.0 B-1/B-2：产品档案 API 测试。

- 创建 / 重 code 409 / 跨 org 隔离 / 更新乐观锁 / 删除（有关联 409、无关联成功）
- facts ?product_id= 筛选 + 存量无 product_id 事实兼容
- 未登录 401 / 无权 404（防枚举）
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """三组织环境：org1(owner u1 + viewer u_viewer)、org2(owner u2)。"""
    url = f"sqlite:///{tmp_path}/products.db"
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

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()


PRODUCT_BODY = {
    "code": "CHERRY-001",
    "name": "烟台大樱桃",
    "category": "水果",
    "origin": "山东烟台",
    "brand_profile": "果园来信品牌",
    "brand_tone": "实在、唠嗑、不端着",
    "selling_points": ["果大", "汁多", "当季鲜"],
    "ad_restrictions": "不宣称医疗功效",
    "no_expand_claims": "不编造销量排名",
}


def _create(client, org_id, **kw):
    body = {"org_id": org_id, **PRODUCT_BODY, **kw}
    return client.post("/api/v1/products", json=body)


class TestProductCRUD:
    def test_create_and_get(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        assert r.status_code == 201, r.text
        data = r.json()["data"]
        assert data["code"] == "CHERRY-001"
        assert data["name"] == "烟台大樱桃"
        assert data["selling_points"] == ["果大", "汁多", "当季鲜"]
        assert data["version"] == 1
        pid = data["id"]

        r = client.get(f"/api/v1/products/{pid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["brand_tone"] == "实在、唠嗑、不端着"

        r = client.get("/api/v1/products", params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 1

    def test_duplicate_code_same_org_409(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        assert r.status_code == 201, r.text
        r = _create(client, api_env.org1)
        assert r.status_code == 409, r.text
        assert r.json().get("code") == 3006  # PRODUCT_CODE_CONFLICT

    def test_same_code_different_org_allowed(self, api_env):
        # 注意：as_user 会替换全局登录覆盖，同一 client 先后以不同身份请求
        client = api_env.as_user("u1")
        r1 = _create(client, api_env.org1)
        assert r1.status_code == 201, r1.text
        client = api_env.as_user("u2")
        r2 = _create(client, api_env.org2)
        assert r2.status_code == 201, r2.text

    def test_cross_org_product_invisible(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        assert r.status_code == 201, r.text
        pid = r.json()["data"]["id"]
        client = api_env.as_user("u2")
        # u2 拿着 org1 的产品 id 查 org2 → 404（租户隔离）
        r = client.get(f"/api/v1/products/{pid}",
                       params={"org_id": api_env.org2})
        assert r.status_code == 404, r.text
        # u2 连 org1 都进不去 → 404（防枚举）
        r = client.get(f"/api/v1/products/{pid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text

    def test_update_and_optimistic_lock(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        pid = r.json()["data"]["id"]
        ver = r.json()["data"]["version"]

        r = client.patch(f"/api/v1/products/{pid}", json={
            "org_id": api_env.org1, "expected_version": ver,
            "name": "烟台大樱桃（新）", "origin": "山东烟台福山",
        })
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["name"] == "烟台大樱桃（新）"
        assert data["version"] == ver + 1
        # code 不可改：传 code 应被忽略
        r = client.patch(f"/api/v1/products/{pid}", json={
            "org_id": api_env.org1, "expected_version": ver + 1,
            "code": "HACKED",
        })
        assert r.status_code == 200, r.text
        assert r.json()["data"]["code"] == "CHERRY-001"

        # 旧版本更新 → 409
        r = client.patch(f"/api/v1/products/{pid}", json={
            "org_id": api_env.org1, "expected_version": ver,
            "name": "并发修改",
        })
        assert r.status_code == 409, r.text
        assert r.json().get("code") == 4090  # VERSION_CONFLICT

    def test_delete_without_facts(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        pid = r.json()["data"]["id"]
        r = client.delete(f"/api/v1/products/{pid}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/v1/products/{pid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text

    def test_delete_with_fact_reference_409(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        pid = r.json()["data"]["id"]
        # 事实用产品 id 引用
        r = client.post("/api/v1/facts", json={
            "org_id": api_env.org1, "content": "樱桃产自烟台福山",
            "product_id": pid, "source_type": "human_statement"})
        assert r.status_code == 201, r.text
        r = client.delete(f"/api/v1/products/{pid}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 409, r.text
        assert r.json().get("code") == 3007  # PRODUCT_HAS_FACTS
        # 产品还在
        r = client.get(f"/api/v1/products/{pid}",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200

    def test_delete_with_fact_code_reference_409(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1, code="CHICKEN-001")
        pid = r.json()["data"]["id"]
        # 事实用产品 code 引用（自由字符串兼容）
        r = client.post("/api/v1/facts", json={
            "org_id": api_env.org1, "content": "扒鸡产自德州",
            "product_id": "CHICKEN-001", "source_type": "human_statement"})
        assert r.status_code == 201, r.text
        r = client.delete(f"/api/v1/products/{pid}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 409, r.text


class TestFactsProductFilter:
    def test_filter_by_product_id(self, api_env):
        client = api_env.as_user("u1")
        r = _create(client, api_env.org1)
        pid = r.json()["data"]["id"]
        # 一条挂产品，一条不挂（存量兼容）
        r = client.post("/api/v1/facts", json={
            "org_id": api_env.org1, "content": "樱桃好吃",
            "product_id": pid, "source_type": "human_statement"})
        assert r.status_code == 201, r.text
        r = client.post("/api/v1/facts", json={
            "org_id": api_env.org1, "content": "通用事实",
            "source_type": "human_statement"})
        assert r.status_code == 201, r.text

        r = client.get("/api/v1/facts",
                       params={"org_id": api_env.org1, "product_id": pid})
        assert r.status_code == 200, r.text
        items = r.json()["data"]
        assert len(items) == 1
        assert items[0]["content"] == "樱桃好吃"
        assert items[0]["product_id"] == pid
        # scope 字段透传（V2.1）
        assert "scope" in items[0]

        # 不带 product_id：两条都可见（存量兼容）
        r = client.get("/api/v1/facts", params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 2

    def test_filter_by_code_string(self, api_env):
        client = api_env.as_user("u1")
        _create(client, api_env.org1, code="BEER-001")
        r = client.post("/api/v1/facts", json={
            "org_id": api_env.org1, "content": "啤酒产自青岛",
            "product_id": "BEER-001", "source_type": "human_statement"})
        assert r.status_code == 201, r.text
        r = client.get("/api/v1/facts",
                       params={"org_id": api_env.org1, "product_id": "BEER-001"})
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 1


class TestProductPermissions:
    def test_unauthorized_401(self, api_env):
        from backend.app import app
        client = api_env.as_user("u1")
        app.dependency_overrides.clear()  # 去掉登录覆盖
        try:
            r = client.get("/api/v1/products",
                           params={"org_id": api_env.org1})
            assert r.status_code == 401, r.text
        finally:
            api_env.as_user("u1")

    def test_viewer_cannot_create(self, api_env):
        client = api_env.as_user("u_viewer")
        r = _create(client, api_env.org1)
        assert r.status_code == 404, r.text  # 防枚举：无权按 404
        assert r.json().get("code") == 4002  # ORG_NOT_FOUND

    def test_viewer_can_read(self, api_env):
        owner = api_env.as_user("u1")
        r = _create(owner, api_env.org1)
        assert r.status_code == 201, r.text
        viewer = api_env.as_user("u_viewer")
        r = viewer.get("/api/v1/products", params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 1
        # viewer 不能删除
        pid = r.json()["data"][0]["id"]
        r = viewer.delete(f"/api/v1/products/{pid}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text
