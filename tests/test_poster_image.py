"""海报 AI 生图测试（待接入 #5）。

- GET /api/v1/poster/image/status：是否配置 key
- POST /api/v1/poster/image：未配置 → 503（前端置灰，不伪造）；
  SSRF 拦截非法 base；上游错误 → 502；成功返回图片数据
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：user=owner。"""
    url = f"sqlite:///{tmp_path}/posterimg.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
    # 确保默认无 key
    monkeypatch.delenv("LUPIN_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("LUPIN_IMAGE_API_BASE", raising=False)
    import backend.auth  # noqa: F401
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.media import reset_storage
    from backend.media.signing import reset_sign_secret
    reset_engine()
    reset_storage()
    reset_sign_secret()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "heads")

    from backend.models import Organization, Membership, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="生图测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local", password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org.id, role="owner"))
    work = Work(org_id=org.id, owner_id="u_owner", title="测试作品",
                data_json='{"product": {}}')
    db.add(work)
    db.commit()
    org_id, work_id = org.id, work.id
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app

    def _override_user():
        return AuthUser(id="u_owner", email="owner@test.local",
                        password_hash="x")

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    # 限流桶按 IP+鉴权头哈希隔离：自带假头，避免与其它测试文件挤占 anon 桶
    client.headers["Authorization"] = "Bearer todo5-test-isolation"
    yield client, org_id, work_id
    app.dependency_overrides.clear()
    reset_engine()
    reset_storage()


class TestImageStatus:
    def test_not_configured(self, api_client):
        client, org_id, _ = api_client
        r = client.get("/api/v1/poster/image/status")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["configured"] is False

    def test_configured(self, api_client, monkeypatch):
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        client, org_id, _ = api_client
        r = client.get("/api/v1/poster/image/status")
        assert r.status_code == 200
        assert r.json()["data"]["configured"] is True

    def test_unauthorized(self, api_client):
        from backend.app import app
        from backend.auth.deps import get_current_user
        client, org_id, _ = api_client
        app.dependency_overrides.pop(get_current_user, None)
        try:
            r = client.get("/api/v1/poster/image/status")
        finally:
            pass
        assert r.status_code in (401, 403)


class TestImageGenerate:
    def test_no_key_503(self, api_client):
        """未配置 key → 503，前端据此置灰按钮，绝不伪造图片。"""
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": org_id, "prompt": "一颗红苹果，国潮风海报",
        })
        assert r.status_code == 503, r.text

    def test_wrong_org_404(self, api_client, monkeypatch):
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        client, _org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": "no-such-org", "prompt": "苹果",
        })
        assert r.status_code == 404

    def test_prompt_validation(self, api_client, monkeypatch):
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": org_id, "prompt": "",
        })
        assert r.status_code == 422

    def test_success_b64(self, api_client, monkeypatch):
        """mock _call_image_api：成功返回 b64。"""
        import backend.api.v1.poster_image as pi
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        monkeypatch.setattr(pi, "_call_image_api",
                            lambda *a, **k: {"b64_json": "AAAABBBB"})
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": org_id, "prompt": "一颗红苹果",
        })
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["image_b64"] == "AAAABBBB"
        assert d["image_url"] is None

    def test_ssrf_blocked(self, api_client, monkeypatch):
        """内网 base 被 SSRF 拦截 → 400。"""
        import backend.api.v1.poster_image as pi
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        monkeypatch.setenv("LUPIN_IMAGE_API_BASE", "http://127.0.0.1:9999")
        # 走真实 _call_image_api（httpx 不会被调用，SSRF 先拦截）
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": org_id, "prompt": "一颗红苹果",
        })
        assert r.status_code == 400, r.text

    def test_upstream_500(self, api_client, monkeypatch):
        """上游 500 → 502，不回传原文。"""
        import httpx
        import backend.api.v1.poster_image as pi
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "sk-test")
        monkeypatch.setenv("LUPIN_IMAGE_API_BASE", "https://example.com")

        class FakeResp:
            status_code = 500
            def json(self): return {"error": "boom"}

        class FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return FakeResp()

        # example.com 需过 SSRF（真实 DNS）；直接 mock validate 放行
        monkeypatch.setattr(pi, "validate_outbound_url", lambda u, **k: u)
        monkeypatch.setattr(httpx, "Client", FakeClient)
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image", json={
            "org_id": org_id, "prompt": "一颗红苹果",
        })
        assert r.status_code == 502, r.text
