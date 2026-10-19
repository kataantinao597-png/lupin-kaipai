"""V3.0 G：跨项目素材库测试。

- 引用只记关系、不复制文件；删引用 ≠ 删实体
- 仍被引用的资产实体 DELETE 被拦截（409/ASSET_HAS_REFS，提示引用数）
- 解除全部引用后可删实体；跨项目检索 org 隔离；引用计数正确
- 删除安全检查为加法拦截，assets 既有删除链路行为不回归
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
    url = f"sqlite:///{tmp_path}/assetlib.db"
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

        def db(self):
            return get_session_factory()()

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()


def _work(env, org_id, title, owner="u1"):
    from backend.models import Work
    s = env.db()
    w = Work(org_id=org_id, owner_id=owner, title=title)
    s.add(w)
    s.commit()
    wid = w.id
    s.close()
    return wid


def _asset(env, org_id, work_id, filename="果园.jpg", mime="image/jpeg"):
    from backend.models import Asset
    s = env.db()
    a = Asset(org_id=org_id, work_id=work_id,
              key=f"{org_id}/{work_id}/{filename.replace('/', '_')}",
              filename=filename, mime=mime, size=1024, sha256=None,
              status="ready", created_by="u1")
    s.add(a)
    s.commit()
    aid = a.id
    key = a.key
    s.close()
    return aid, key


def _seed(env):
    """项目 A（樱桃宣传）有 2 个素材，项目 B（扒鸡宣传）为空。"""
    wa = _work(env, env.org1, "樱桃宣传片")
    wb = _work(env, env.org1, "扒鸡宣传片")
    wc = _work(env, env.org2, "啤酒宣传片")
    aid1, _ = _asset(env, env.org1, wa, "果园.jpg", "image/jpeg")
    aid2, _ = _asset(env, env.org1, wa, "采摘.mp4", "video/mp4")
    return wa, wb, wc, aid1, aid2


def _ref(client, org_id, asset_id, dest_work_id):
    return client.post("/api/v1/asset-library/refs",
                       params={}, json={
                           "org_id": org_id, "asset_id": asset_id,
                           "dest_work_id": dest_work_id})


class TestCreateRef:
    def test_create_ref_ok(self, api_env):
        wa, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        r = _ref(c, api_env.org1, aid1, wb)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        assert d["asset_id"] == aid1
        assert d["dest_work_id"] == wb
        assert d["source_work_id"] == wa
        assert d["source_work_title"] == "樱桃宣传片"
        assert d["dest_work_title"] == "扒鸡宣传片"

    def test_ref_does_not_copy_file(self, api_env):
        """引用只记关系：资产行不新增，key/大小不变。"""
        wa, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        from backend.models import Asset
        s = api_env.db()
        before = s.query(Asset).filter_by(org_id=api_env.org1).count()
        key_before = s.query(Asset).filter_by(id=aid1).first().key
        s.close()
        r = _ref(c, api_env.org1, aid1, wb)
        assert r.status_code == 201, r.text
        s = api_env.db()
        assert s.query(Asset).filter_by(org_id=api_env.org1).count() == before
        assert s.query(Asset).filter_by(id=aid1).first().key == key_before
        s.close()

    def test_duplicate_ref_409(self, api_env):
        _, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        r = _ref(c, api_env.org1, aid1, wb)
        assert r.status_code == 201, r.text
        r = _ref(c, api_env.org1, aid1, wb)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == 3018

    def test_self_ref_400(self, api_env):
        wa, _, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        r = _ref(c, api_env.org1, aid1, wa)
        assert r.status_code == 400, r.text

    def test_ref_missing_asset_404(self, api_env):
        _, wb, _, _, _ = _seed(api_env)
        c = api_env.as_user("u1")
        r = _ref(c, api_env.org1, "deadbeef" * 4, wb)
        assert r.status_code == 404, r.text

    def test_viewer_cannot_create_ref(self, api_env):
        """viewer 无 asset:upload → 404（防枚举口径）。"""
        _, wb, _, aid1, _ = _seed(api_env)
        cv = api_env.as_user("u_viewer")
        r = _ref(cv, api_env.org1, aid1, wb)
        assert r.status_code == 404, r.text

    def test_cross_org_ref_blocked(self, api_env):
        """org2 的 owner 不能引用 org1 的素材（资产查不到 → 404）。"""
        wa, _, wc, aid1, _ = _seed(api_env)
        c2 = api_env.as_user("u2")
        r = _ref(c2, api_env.org2, aid1, wc)
        assert r.status_code == 404, r.text


class TestDeleteSafety:
    def test_delete_blocked_while_referenced(self, api_env):
        """仍被引用的资产实体 DELETE → 409/ASSET_HAS_REFS，提示引用数。"""
        _, wb, _, aid1, aid2 = _seed(api_env)
        c = api_env.as_user("u1")
        assert _ref(c, api_env.org1, aid1, wb).status_code == 201
        r = c.delete(f"/api/v1/assets/{aid1}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == 3019
        assert "1" in body["message"]  # 引用数提示
        # 实体仍在
        r2 = c.get(f"/api/v1/asset-library/assets/{aid1}",
                   params={"org_id": api_env.org1})
        assert r2.status_code == 200, r2.text

    def test_delete_blocked_reports_count(self, api_env):
        """2 个项目引用时，拦截提示引用数为 2。"""
        wa, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        wc = _work(api_env, api_env.org1, "啤酒宣传片")
        assert _ref(c, api_env.org1, aid1, wb).status_code == 201
        assert _ref(c, api_env.org1, aid1, wc).status_code == 201
        r = c.delete(f"/api/v1/assets/{aid1}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 409, r.text
        assert "2" in r.json()["message"]

    def test_delete_after_all_refs_removed(self, api_env):
        """解除全部引用后可正常删除实体（现有删除链路不回归）。"""
        _, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        ref_id = _ref(c, api_env.org1, aid1, wb).json()["data"]["id"]
        r = c.delete(f"/api/v1/asset-library/refs/{ref_id}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = c.delete(f"/api/v1/assets/{aid1}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "deleted"
        # 已删除资产从库检索中消失
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "q": "果园"})
        assert all(i["id"] != aid1 for i in r.json()["data"]["items"])

    def test_remove_ref_does_not_delete_entity(self, api_env):
        """删引用 ≠ 删实体：解除引用后资产仍可用。"""
        _, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        ref_id = _ref(c, api_env.org1, aid1, wb).json()["data"]["id"]
        r = c.delete(f"/api/v1/asset-library/refs/{ref_id}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = c.get(f"/api/v1/asset-library/assets/{aid1}",
                  params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["ref_count"] == 0

    def test_remove_nonexistent_ref_404(self, api_env):
        c = api_env.as_user("u1")
        r = c.delete(f"/api/v1/asset-library/refs/{'0' * 32}",
                     params={"org_id": api_env.org1})
        assert r.status_code == 404, r.text
        assert r.json()["code"] == 3017


class TestSearchAndDetail:
    def test_search_org_isolated(self, api_env):
        """org2 的库检索看不到 org1 的素材。"""
        _seed(api_env)
        c2 = api_env.as_user("u2")
        r = c2.get("/api/v1/asset-library/search",
                   params={"org_id": api_env.org2})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["items"] == []
        # org1 的库里有 2 个
        c1 = api_env.as_user("u1")
        r = c1.get("/api/v1/asset-library/search",
                   params={"org_id": api_env.org1})
        assert len(r.json()["data"]["items"]) == 2

    def test_search_by_name_and_mime(self, api_env):
        _, wb, _, aid1, aid2 = _seed(api_env)
        c = api_env.as_user("u1")
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "q": "果园"})
        items = r.json()["data"]["items"]
        assert [i["id"] for i in items] == [aid1]
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "mime_type": "video"})
        items = r.json()["data"]["items"]
        assert [i["id"] for i in items] == [aid2]
        assert items[0]["work_title"] == "樱桃宣传片"

    def test_search_by_work(self, api_env):
        wa, _, _, aid1, aid2 = _seed(api_env)
        c = api_env.as_user("u1")
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "work_id": wa})
        assert len(r.json()["data"]["items"]) == 2
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "work_id": "0" * 32})
        assert r.json()["data"]["items"] == []

    def test_ref_count_correct(self, api_env):
        """引用计数：被 2 个项目引用 → detail 与 search 卡片均为 2。"""
        _, wb, _, aid1, aid2 = _seed(api_env)
        c = api_env.as_user("u1")
        wc = _work(api_env, api_env.org1, "啤酒宣传片")
        _ref(c, api_env.org1, aid1, wb)
        _ref(c, api_env.org1, aid1, wc)
        r = c.get(f"/api/v1/asset-library/assets/{aid1}",
                  params={"org_id": api_env.org1})
        assert r.json()["data"]["ref_count"] == 2
        r = c.get("/api/v1/asset-library/search",
                  params={"org_id": api_env.org1, "q": "果园"})
        assert r.json()["data"]["items"][0]["ref_count"] == 2
        r = c.get(f"/api/v1/asset-library/assets/{aid2}",
                  params={"org_id": api_env.org1})
        assert r.json()["data"]["ref_count"] == 0

    def test_detail_shows_source_and_ref_projects(self, api_env):
        """详情展示来源项目 + 引用项目（哪几个项目在用）。"""
        _, wb, _, aid1, _ = _seed(api_env)
        c = api_env.as_user("u1")
        _ref(c, api_env.org1, aid1, wb)
        r = c.get(f"/api/v1/asset-library/assets/{aid1}",
                  params={"org_id": api_env.org1})
        d = r.json()["data"]
        assert d["source_work_title"] == "樱桃宣传片"
        assert len(d["refs"]) == 1
        assert d["refs"][0]["dest_work_title"] == "扒鸡宣传片"

    def test_refs_by_work(self, api_env):
        """项目 B 视角：列出它引用的全部外来素材。"""
        _, wb, _, aid1, aid2 = _seed(api_env)
        c = api_env.as_user("u1")
        _ref(c, api_env.org1, aid1, wb)
        _ref(c, api_env.org1, aid2, wb)
        r = c.get(f"/api/v1/asset-library/works/{wb}/refs",
                  params={"org_id": api_env.org1})
        items = r.json()["data"]["items"]
        assert len(items) == 2
        assert {i["asset_id"] for i in items} == {aid1, aid2}
        assert all(i["source_work_title"] == "樱桃宣传片" for i in items)
