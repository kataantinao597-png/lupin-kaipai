"""S8：媒体存储与处理管线测试。

- 单元：魔数识别/大小限制、HMAC 签名、图片头解析、本地存储（含穿越防护）
- 集成：上传意图 → 文件上传（小 PNG 实测）→ 元数据提取 → 签名下载全链路、
  时间轴差值 API、shots 参考图版权字段
"""
import hashlib
import os
import struct
import sys
import time

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402

from backend.media import validation, signing  # noqa: E402
from backend.media.probe import image_dimensions  # noqa: E402
from backend.media.storage import sanitize_key  # noqa: E402
from backend.media.local_driver import LocalStorageBackend  # noqa: E402


# ---------- 测试图片构造 ----------

def _make_png(w: int = 3, h: int = 2) -> bytes:
    # 最小 PNG：签名 + IHDR（宽高）+ IEND（CRC 不校验，只测头解析）
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
            + struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00")


def _make_jpeg(w: int = 5, h: int = 4) -> bytes:
    # SOI + APP0 + SOF0（precision/高/宽）+ EOI
    return (b"\xff\xd8"
            b"\xff\xe0\x00\x10" + b"JFIF\x00" + b"\x00" * 9 +
            b"\xff\xc0\x00\x0b\x08"
            + struct.pack(">H", h) + struct.pack(">H", w)
            + b"\x01\x01\x11\x00"
            + b"\xff\xd9")


# ================= 单元：validation =================

class TestValidation:
    def test_detect_png(self):
        assert validation.detect_mime(_make_png()) == ("image/png", "image")

    def test_detect_jpeg(self):
        assert validation.detect_mime(_make_jpeg()) == ("image/jpeg", "image")

    def test_detect_gif(self):
        assert validation.detect_mime(b"GIF89a" + b"\x00" * 30) == \
            ("image/gif", "image")

    def test_detect_webp(self):
        assert validation.detect_mime(b"RIFF" + b"\x00" * 4 + b"WEBP" +
                                      b"\x00" * 20) == ("image/webp", "image")

    def test_detect_mp4(self):
        assert validation.detect_mime(b"\x00\x00\x00\x18ftypmp42" +
                                      b"\x00" * 20) == ("video/mp4", "video")

    def test_detect_zip(self):
        assert validation.detect_mime(b"PK\x03\x04" + b"\x00" * 30) == \
            ("application/zip", "archive")

    def test_detect_unknown(self):
        assert validation.detect_mime(b"\x00\x01\x02\x03" * 10) is None
        assert validation.detect_mime(b"") is None

    def test_validate_ok(self):
        mime, cat = validation.validate_upload(_make_png(), "image/png")
        assert (mime, cat) == ("image/png", "image")

    def test_validate_rejects_mismatch(self):
        # 声明是视频，实际是 PNG → 拒绝
        with pytest.raises(validation.MediaValidationError):
            validation.validate_upload(_make_png(), "video/mp4")

    def test_validate_rejects_unknown(self):
        with pytest.raises(validation.MediaValidationError):
            validation.validate_upload(b"\x00" * 64, "")

    def test_validate_rejects_oversize(self):
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1)
        with pytest.raises(validation.MediaValidationError):
            validation.validate_upload(big, "image/png")


# ================= 单元：signing =================

class TestSigning:
    def test_sign_verify_roundtrip(self):
        signing.reset_sign_secret()
        p = signing.make_signed_params("asset123")
        assert signing.verify("asset123", p["exp"], p["sig"]) is True

    def test_verify_wrong_asset(self):
        signing.reset_sign_secret()
        p = signing.make_signed_params("asset123")
        assert signing.verify("asset999", p["exp"], p["sig"]) is False

    def test_verify_expired(self):
        signing.reset_sign_secret()
        past = int(time.time()) - 10
        sig = signing.sign("asset123", past)
        assert signing.verify("asset123", past, sig) is False

    def test_verify_tampered(self):
        signing.reset_sign_secret()
        p = signing.make_signed_params("asset123")
        assert signing.verify("asset123", p["exp"], p["sig"] + "x") is False


# ================= 单元：probe =================

class TestProbe:
    def test_png_dimensions(self):
        d = image_dimensions(_make_png(3, 2), "image/png")
        assert (d["width"], d["height"]) == (3, 2)

    def test_jpeg_dimensions(self):
        d = image_dimensions(_make_jpeg(5, 4), "image/jpeg")
        assert (d["width"], d["height"]) == (5, 4)

    def test_gif_dimensions(self):
        data = b"GIF89a" + struct.pack("<HH", 7, 9) + b"\x00" * 30
        d = image_dimensions(data, "image/gif")
        assert (d["width"], d["height"]) == (7, 9)

    def test_corrupt_returns_none(self):
        d = image_dimensions(b"\x89PNG\r\n\x1a\n", "image/png")
        assert d["width"] is None and d["height"] is None


# ================= 单元：storage =================

class TestLocalStorage:
    def test_put_get_delete(self, tmp_path):
        s = LocalStorageBackend(root=str(tmp_path / "m"))
        s.put("org1/work1/a.bin", b"hello")
        assert s.get("org1/work1/a.bin") == b"hello"
        assert s.exists("org1/work1/a.bin") is True
        assert s.local_path("org1/work1/a.bin") is not None
        s.delete("org1/work1/a.bin")
        assert s.exists("org1/work1/a.bin") is False
        s.delete("org1/work1/a.bin")  # 幂等，不抛错

    def test_traversal_blocked(self, tmp_path):
        s = LocalStorageBackend(root=str(tmp_path / "m"))
        for bad in ["../evil", "a/../../evil", "/abs/path", "a//b", ""]:
            with pytest.raises(ValueError):
                sanitize_key(bad)
            with pytest.raises(ValueError):
                s.put(bad, b"x")

    def test_get_missing(self, tmp_path):
        s = LocalStorageBackend(root=str(tmp_path / "m"))
        with pytest.raises(FileNotFoundError):
            s.get("nope.bin")


# ================= 集成：全链路 =================

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：user=owner。媒体目录指向 tmp。"""
    url = f"sqlite:///{tmp_path}/media.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
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
    # 并行组产生了多 head（0007_security_phase9 / 0009_enterprise），用 heads 全升
    command.upgrade(cfg, "heads")

    from backend.models import Organization, Membership, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="媒体测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local", password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org.id, role="owner"))
    work = Work(org_id=org.id, owner_id="u_owner", title="测试作品",
                data_json='{"product": {"shoot": {"target_duration_seconds": 60}}}')
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
    # 裸 TestClient 不触发 startup 事件，显式启动媒体处理线程（测真实链路）
    from backend.media.worker import start_media_worker, stop_media_worker
    start_media_worker()
    client = TestClient(app, raise_server_exceptions=False)
    yield client, org_id, work_id
    app.dependency_overrides.clear()
    stop_media_worker()
    reset_engine()
    reset_storage()


def _wait_for_meta(client, org_id, asset_id, timeout=20.0):
    """轮询资产详情直到元数据就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v1/assets/{asset_id}",
                       params={"org_id": org_id})
        assert r.status_code == 200, r.text
        meta = r.json()["data"]["meta"]
        if meta.get("width") is not None:
            return meta
        time.sleep(0.5)
    raise AssertionError("元数据提取超时")


class TestMediaChain:
    def test_full_chain_png(self, api_client):
        client, org_id, work_id = api_client
        png = _make_png(3, 2)
        digest = hashlib.sha256(png).hexdigest()

        # 1. 上传意图
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "test.png", "mime": "image/png",
            "size": len(png), "sha256": digest})
        assert r.status_code == 201, r.text
        intent = r.json()["data"]
        token = intent["upload_token"]
        assert intent["key"].startswith(f"{org_id}/{work_id}/")

        # 2. 上传文件（multipart）
        r = client.post("/api/v1/assets/upload",
                        data={"upload_token": token},
                        files={"file": ("test.png", png, "image/png")})
        assert r.status_code == 201, r.text
        asset = r.json()["data"]
        assert asset["status"] == "ready"
        assert asset["mime"] == "image/png"
        assert asset["sha256"] == digest
        asset_id = asset["id"]

        # 3. token 一次有效：重用被拒
        r = client.post("/api/v1/assets/upload",
                        data={"upload_token": token},
                        files={"file": ("test.png", png, "image/png")})
        assert r.status_code == 400

        # 4. 等待后台线程提取元数据
        meta = _wait_for_meta(client, org_id, asset_id)
        assert meta["width"] == 3 and meta["height"] == 2

        # 5. 列表
        r = client.get("/api/v1/assets",
                       params={"org_id": org_id, "work_id": work_id})
        assert r.status_code == 200, r.text
        assert any(a["id"] == asset_id for a in r.json()["data"])

        # 6. 签名下载
        r = client.get(f"/api/v1/assets/{asset_id}/download",
                       params={"org_id": org_id})
        assert r.status_code == 200, r.text
        url = r.json()["data"]["url"]
        assert "/raw?exp=" in url and "&sig=" in url

        # 7. raw 下载（免登录，验签）
        r = client.get(url)
        assert r.status_code == 200, r.text[:200]
        assert r.content == png

        # 8. 篡改签名被拒
        r = client.get(url + "x")
        assert r.status_code == 403

    def test_upload_rejects_bad_magic(self, api_client):
        client, org_id, work_id = api_client
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "evil.png", "mime": "image/png", "size": 64})
        assert r.status_code == 201, r.text
        token = r.json()["data"]["upload_token"]
        # 实际内容是文本，魔数不符
        r = client.post("/api/v1/assets/upload",
                        data={"upload_token": token},
                        files={"file": ("evil.png", b"hello world" * 6,
                                        "image/png")})
        assert r.status_code == 400
        assert "校验失败" in r.json()["message"]

    def test_upload_rejects_hash_mismatch(self, api_client):
        client, org_id, work_id = api_client
        png = _make_png()
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "a.png", "mime": "image/png",
            "size": len(png), "sha256": "0" * 64})
        assert r.status_code == 201, r.text
        token = r.json()["data"]["upload_token"]
        r = client.post("/api/v1/assets/upload",
                        data={"upload_token": token},
                        files={"file": ("a.png", png, "image/png")})
        assert r.status_code == 400
        assert "哈希" in r.json()["message"]

    def test_delete_asset(self, api_client):
        client, org_id, work_id = api_client
        png = _make_png()
        r = client.post("/api/v1/assets/upload-intents", json={
            "org_id": org_id, "work_id": work_id,
            "filename": "d.png", "mime": "image/png", "size": len(png)})
        token = r.json()["data"]["upload_token"]
        r = client.post("/api/v1/assets/upload",
                        data={"upload_token": token},
                        files={"file": ("d.png", png, "image/png")})
        asset_id = r.json()["data"]["id"]
        r = client.delete(f"/api/v1/assets/{asset_id}",
                          params={"org_id": org_id})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/v1/assets/{asset_id}",
                       params={"org_id": org_id})
        assert r.status_code == 404


class TestTimeline:
    def test_timeline_diff(self, api_client):
        client, org_id, work_id = api_client
        from backend.auth.database import get_session_factory
        from backend.models import Storyboard, Shot
        db = get_session_factory()()
        sb = Storyboard(work_id=work_id, title="v1")
        db.add(sb)
        db.flush()
        # 3 个镜头共 50s，目标 60s → 差 -10s
        for i, dur in enumerate([20, 20, 10], start=1):
            db.add(Shot(storyboard_id=sb.id, shot_no=i,
                        shot_id_stable=f"s{i}", duration_seconds=dur,
                        ref_image_copyright="AI 生成参考",
                        ref_image_source="lupin-ai",
                        ref_image_license="internal"))
        db.commit()
        db.close()

        r = client.get(f"/api/v1/works/{work_id}/timeline")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["target_duration_seconds"] == 60
        assert data["total_duration_seconds"] == 50
        assert data["diff_seconds"] == -10
        assert data["within_tolerance"] is False
        assert data["shot_count"] == 3
        assert data["shots"][0]["duration_seconds"] == 20

    def test_timeline_within_tolerance(self, api_client):
        client, org_id, work_id = api_client
        from backend.auth.database import get_session_factory
        from backend.models import Storyboard, Shot
        db = get_session_factory()()
        sb = Storyboard(work_id=work_id, title="v1")
        db.add(sb)
        db.flush()
        db.add(Shot(storyboard_id=sb.id, shot_no=1, duration_seconds=61))
        db.commit()
        db.close()
        r = client.get(f"/api/v1/works/{work_id}/timeline")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["within_tolerance"] is True

    def test_shot_ref_fields_persisted(self, api_client):
        """S8.3：shots 参考图版权字段落库可读。"""
        client, org_id, work_id = api_client
        from backend.auth.database import get_session_factory
        from backend.models import Storyboard, Shot
        db = get_session_factory()()
        sb = Storyboard(work_id=work_id, title="v1")
        db.add(sb)
        db.flush()
        shot = Shot(storyboard_id=sb.id, shot_no=1, duration_seconds=5,
                    ref_image_copyright="© 星霄",
                    ref_image_source="https://example.com/x.png",
                    ref_image_license="CC-BY")
        db.add(shot)
        db.commit()
        sid = shot.id
        db.close()
        db = get_session_factory()()
        s = db.get(Shot, sid)
        assert s.ref_image_copyright == "© 星霄"
        assert s.ref_image_source == "https://example.com/x.png"
        assert s.ref_image_license == "CC-BY"
        db.close()
