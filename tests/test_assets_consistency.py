"""V2.1 上传一致性测试：commit 失败清孤儿文件 / 文件丢失返回 410。"""
import hashlib
import json
import os
import struct
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402


def _make_png(w: int = 3, h: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
            + struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00")


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/api.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.media.storage import reset_storage
    reset_engine()
    reset_storage()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="一致性测试社", created_by="u_owner")
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
    ids = {"org": org.id, "work": work.id}
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
    client.headers.update({"Authorization": "Bearer <redacted>"})
    yield client, ids
    app.dependency_overrides.clear()
    reset_storage()


def _make_intent(client, org_id, work_id, png):
    digest = hashlib.sha256(png).hexdigest()
    r = client.post("/api/v1/assets/upload-intents", json={
        "org_id": org_id, "work_id": work_id,
        "filename": "test.png", "mime": "image/png",
        "size": len(png), "sha256": digest})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_upload_commit_failure_cleans_orphan(api_client, monkeypatch):
    """db.commit 失败 → 存储孤儿文件被清理，不留垃圾。"""
    client, ids = api_client
    from sqlalchemy.orm import Session as SASession
    from backend.media.storage import get_storage

    png = _make_png()
    intent = _make_intent(client, ids["org"], ids["work"], png)
    token = intent["upload_token"]
    key = intent["key"]

    real_commit = SASession.commit

    def boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(SASession, "commit", boom)
    r = client.post("/api/v1/assets/upload",
                    data={"upload_token": token},
                    files={"file": ("test.png", png, "image/png")})
    monkeypatch.setattr(SASession, "commit", real_commit)
    assert r.status_code == 500, r.text
    # 孤儿文件已被清理
    assert not get_storage().exists(key)


def test_download_missing_file_returns_410(api_client):
    """DB 有记录但物理文件丢失 → 410（不是 500）。"""
    client, ids = api_client
    from backend.models import Asset
    from backend.auth.database import get_session_factory
    from backend.media import signing

    db = get_session_factory()()
    try:
        a = Asset(org_id=ids["org"], work_id=ids["work"],
                  filename="ghost.png", mime="image/png", size=10,
                  key=f"{ids['org']}/{ids['work']}/ghost.png",
                  status="ready", created_by="u_owner")
        db.add(a)
        db.commit()
        asset_id = a.id
    finally:
        db.close()

    params = signing.make_signed_params(asset_id)
    r = client.get(f"/api/v1/assets/{asset_id}/raw",
                   params={"exp": params["exp"], "sig": params["sig"]})
    assert r.status_code == 410, r.text
    assert "文件已丢失" in r.text


def test_upload_and_download_happy_path(api_client):
    """正常上传 → 签名下载 200（exists 检查不破坏正常链路）。"""
    client, ids = api_client
    from backend.media import signing

    png = _make_png()
    intent = _make_intent(client, ids["org"], ids["work"], png)
    r = client.post("/api/v1/assets/upload",
                    data={"upload_token": intent["upload_token"]},
                    files={"file": ("test.png", png, "image/png")})
    assert r.status_code == 201, r.text
    asset_id = r.json()["data"]["id"]

    params = signing.make_signed_params(asset_id)
    r = client.get(f"/api/v1/assets/{asset_id}/raw",
                   params={"exp": params["exp"], "sig": params["sig"]})
    assert r.status_code == 200, r.text
    assert r.content == png
