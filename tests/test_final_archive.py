"""成片上传归档测试（待接入 #2）。

复用现有 assets 接口（S8.1/S8.2），验证 v6 前端将使用的完整链路：
上传意图 → 上传 MP4 → 按作品列出 → 签名下载 → 删除。
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402

# 最小合法 MP4：box size + "ftyp"
FAKE_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4096


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/archive.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
    import backend.auth  # noqa: F401
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.media import reset_storage
    from backend.media.signing import reset_sign_secret
    from backend.media.worker import start_media_worker, stop_media_worker
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
    org = Organization(name="归档测试社", created_by="u_owner")
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
    start_media_worker()
    client = TestClient(app, raise_server_exceptions=False)
    # 限流桶按 IP+鉴权头哈希隔离：自带假头，避免与其它测试文件挤占 anon 桶
    client.headers["Authorization"] = "Bearer todo5-test-isolation"
    yield client, org_id, work_id
    app.dependency_overrides.clear()
    stop_media_worker()
    reset_engine()
    reset_storage()


def _upload_mp4(client, org_id, work_id, filename="成片.mp4"):
    r = client.post("/api/v1/assets/upload-intents", json={
        "org_id": org_id, "work_id": work_id,
        "filename": filename, "mime": "video/mp4", "size": len(FAKE_MP4),
    })
    assert r.status_code == 201, r.text
    token = r.json()["data"]["upload_token"]
    r2 = client.post("/api/v1/assets/upload",
                     data={"upload_token": token},
                     files={"file": (filename, FAKE_MP4, "video/mp4")})
    assert r2.status_code == 201, r2.text
    return r2.json()["data"]


class TestFinalArchiveFlow:
    def test_upload_intent_rejects_oversize(self, api_client):
        client, org_id, work_id = api_client
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "big.mp4", "mime": "video/mp4",
            "size": 300 * 1024 * 1024,  # 超过 200MB 上限
        })
        assert r.status_code == 400

    def test_archive_flow(self, api_client):
        """上传 → 列表可见 → 签名下载 → 删除。"""
        client, org_id, work_id = api_client
        asset = _upload_mp4(client, org_id, work_id)
        assert asset["mime"] == "video/mp4"
        assert asset["work_id"] == work_id
        asset_id = asset["id"]

        # 按作品列出
        r = client.get("/api/v1/assets",
                       params={"org_id": org_id, "work_id": work_id})
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()["data"]]
        assert asset_id in ids

        # 签发下载 URL 并取文件
        r = client.get(f"/api/v1/assets/{asset_id}/download",
                       params={"org_id": org_id})
        assert r.status_code == 200, r.text
        url = r.json()["data"]["url"]
        assert "/raw" in url
        r = client.get(url)
        assert r.status_code == 200
        assert r.content[:8] == FAKE_MP4[:8]

        # 删除后列表不可见
        r = client.delete(f"/api/v1/assets/{asset_id}",
                          params={"org_id": org_id})
        assert r.status_code == 200
        r = client.get("/api/v1/assets",
                       params={"org_id": org_id, "work_id": work_id})
        assert asset_id not in [a["id"] for a in r.json()["data"]]

    def test_upload_rejects_non_video(self, api_client):
        """声明 video/mp4 但魔数不符 → 拒绝。"""
        client, org_id, work_id = api_client
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "fake.mp4", "mime": "video/mp4", "size": 100,
        })
        token = r.json()["data"]["upload_token"]
        r2 = client.post("/api/v1/assets/upload",
                         data={"upload_token": token},
                         files={"file": ("fake.mp4", b"not a video" * 10,
                                         "video/mp4")})
        assert r2.status_code == 400

    def test_cross_org_blocked(self, api_client):
        """他人工单的 work_id 不能挂载。"""
        client, org_id, work_id = api_client
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": "no-such-work",
            "filename": "x.mp4", "mime": "video/mp4", "size": 100,
        })
        assert r.status_code == 404
