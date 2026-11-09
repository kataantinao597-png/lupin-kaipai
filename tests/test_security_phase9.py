"""Phase 9：安全纵深防御测试。

- 9-1 安全响应头中间件 / CSP 上报端点
- 9-2 SSRF 校验（内网 IP / userinfo / scheme / DNS 解析 / 重定向跟随）
- 9-3 分级限流（档位匹配 / 桶行为 / 429 + Retry-After）
- 9-4 API Key 签发与校验（只存 hash / 撤销 / 过期 / 明文一次性）
- 9-5 审计日志（写入 / 查询过滤）
- 9-6 上传魔数校验（类型识别 / 未知拒绝 / 大小分级）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，避开循环导入

from backend.security import ssrf  # noqa: E402
from backend.security.headers import (  # noqa: E402
    CSP_REPORT_ONLY, SecurityHeadersMiddleware,
)
from backend.security.rate_limit import (  # noqa: E402
    RateLimitMiddleware, _identity_for, _tier_for,
)
from backend.security.upload import (  # noqa: E402
    SIZE_LIMITS, UploadRejected, sniff_kind, validate_upload,
)


# ================= 9-1：安全响应头 =================

def _dummy_asgi_app():
    from starlette.responses import JSONResponse

    async def _app(scope, receive, send):
        resp = JSONResponse({"ok": True})
        await resp(scope, receive, send)
    return _app


def test_security_headers_present():
    from fastapi.testclient import TestClient
    client = TestClient(SecurityHeadersMiddleware(_dummy_asgi_app()))
    r = client.get("/anything")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]
    csp = r.headers["Content-Security-Policy-Report-Only"]
    assert "report-uri /api/v1/security/csp-report" in csp
    assert "frame-ancestors 'none'" in csp
    # report-only：不断言拦截行为，只确认头存在
    assert "Content-Security-Policy" not in r.headers


def test_csp_report_endpoint():
    from fastapi.testclient import TestClient
    from backend.app import app
    client = TestClient(app, raise_server_exceptions=False)
    # 健康检查带安全头（走完整中间件栈）
    r = client.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    # CSP 上报端点可用
    r = client.post("/api/v1/security/csp-report",
                    json={"csp-report": {"blocked-uri": "https://evil.example",
                                        "violated-directive": "script-src",
                                        "document-uri": "/workbench"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ================= 9-2：SSRF =================

class TestSSRF:
    def test_scheme_blocked(self):
        for bad in ["ftp://example.com/x", "file:///etc/passwd",
                    "gopher://example.com/", ""]:
            with pytest.raises(ssrf.SSRFError):
                ssrf.validate_outbound_url(bad)

    def test_userinfo_blocked(self):
        with pytest.raises(ssrf.SSRFError):
            ssrf.validate_outbound_url("https://user:pass@example.com/")

    def test_literal_private_ips_blocked(self):
        for bad in ["http://127.0.0.1/", "http://127.0.0.1:8080/x",
                    "http://10.0.0.5/", "http://172.16.3.9/",
                    "http://192.168.1.1/", "http://169.254.169.254/",
                    "http://[::1]/", "http://0.0.0.0/"]:
            with pytest.raises(ssrf.SSRFError):
                ssrf.validate_outbound_url(bad)

    def test_hostname_resolving_to_private_blocked(self, monkeypatch):
        # 模拟 DNS 把域名解析到内网
        def fake_getaddrinfo(host, port, proto=0, *a, **k):
            return [(2, 1, 6, "", ("10.9.9.9", 0))]
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
        ssrf._clear_dns_cache()
        with pytest.raises(ssrf.SSRFError):
            ssrf.validate_outbound_url("https://model.example.com/v1")

    def test_public_host_passes(self, monkeypatch):
        def fake_getaddrinfo(host, port, proto=0, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
        ssrf._clear_dns_cache()
        url = "https://api.public.example/v1/chat/completions"
        assert ssrf.validate_outbound_url(url) == url

    def test_dns_cache_rebinding(self, monkeypatch):
        calls = []

        def fake_getaddrinfo(host, port, proto=0, *a, **k):
            calls.append(host)
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
        ssrf._clear_dns_cache()
        ssrf.resolve_host("cdn.example.com")
        ssrf.resolve_host("cdn.example.com")
        assert calls == ["cdn.example.com"]  # 第二次命中缓存，未再解析

    def test_fetch_follows_redirect_with_validation(self, monkeypatch):
        # 跳转到内网必须被拦截
        monkeypatch.setattr(ssrf.socket, "getaddrinfo",
                            lambda h, p, proto=0, *a, **k:
                            [(2, 1, 6, "", ("93.184.216.34", 0))])
        ssrf._clear_dns_cache()

        class FakeResp:
            status_code = 302
            headers = {"Location": "http://127.0.0.1/secret"}

        class FakeSession:
            def request(self, *a, **k):
                return FakeResp()

        with pytest.raises(ssrf.SSRFError):
            ssrf.fetch_url_safe("https://start.example.com/",
                                session=FakeSession())

    def test_fetch_returns_normal_response(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo",
                            lambda h, p, proto=0, *a, **k:
                            [(2, 1, 6, "", ("93.184.216.34", 0))])
        ssrf._clear_dns_cache()

        class FakeResp:
            status_code = 200
            headers = {}

        class FakeSession:
            def request(self, *a, **k):
                return FakeResp()

        resp = ssrf.fetch_url_safe("https://api.example.com/x",
                                   session=FakeSession())
        assert resp.status_code == 200


# ================= 9-3：分级限流 =================

class TestRateLimit:
    def test_tier_matching(self):
        assert _tier_for("/api/v1/auth/login") == ("auth", 5)
        assert _tier_for("/api/v1/auth/register") == ("auth", 5)
        # V3.2：ai 档只拦生成类 POST；GET 轮询/查询走 default 档
        # （Agent 面板 2.5s 轮询若计入 ai 档 20/min 会自己打满限流）
        assert _tier_for("/api/ideas", "POST") == ("ai", 20)
        assert _tier_for("/api/v1/ai-tasks", "POST") == ("ai", 20)
        assert _tier_for("/api/storyboard", "POST") == ("ai", 20)
        assert _tier_for("/api/v1/agent/one-shot", "POST") == ("ai", 20)
        assert _tier_for("/api/v1/agent/runs/t1", "GET") == ("default", 100)
        assert _tier_for("/api/ideas", "GET") == ("default", 100)
        assert _tier_for("/api/v1/works") == ("default", 100)
        # 豁免路径
        assert _tier_for("/api/health") is None
        assert _tier_for("/api/ready") is None
        assert _tier_for("/api/v1/metrics") is None
        assert _tier_for("/api/v1/security/csp-report") is None
        assert _tier_for("/") is None
        assert _tier_for("/static/js/core.js") is None

    def test_identity_auth_tier_ip_only(self):
        class Req:
            headers = {}
            client = type("C", (), {"host": "1.2.3.4"})()
        assert _identity_for(Req(), "auth") == "auth:1.2.3.4"

    def test_identity_ai_tier_per_user(self):
        class Req:
            client = type("C", (), {"host": "1.2.3.4"})()

            def __init__(self, authz):
                self.headers = {"authorization": authz}
        a = _identity_for(Req("Bearer AAA"), "ai")
        b = _identity_for(Req("Bearer BBB"), "ai")
        c = _identity_for(Req("Bearer AAA"), "ai")
        assert a != b and a == c  # 不同用户分桶

    def test_identity_cookie_fallback_b02(self):
        """B-02：无 Authorization 时按 lupin_refresh Cookie 分桶；
        X-Forwarded-For 首跳作真实 IP。"""
        class Req:
            client = type("C", (), {"host": "203.0.113.9"})()

            def __init__(self, cookie, xff=None):
                self.headers = {"x-forwarded-for": xff} if xff else {}
                self.cookies = {"lupin_refresh": cookie} if cookie else {}
        a = _identity_for(Req("token-user-A"), "ai")
        b = _identity_for(Req("token-user-B"), "ai")
        assert a != b  # 不同 Cookie 用户不再共用 anon 桶
        assert _identity_for(Req("token-user-A"), "ai") == a
        assert _identity_for(Req(""), "ai").endswith(":anon")
        c = _identity_for(Req("token-user-A", "198.51.100.7, 203.0.113.9"),
                          "ai")
        assert c.startswith("ai:198.51.100.7:")
        # auth 档不信任 XFF（防伪造换桶撞库），仍用直连 IP
        assert _identity_for(
            Req("token-user-A", "198.51.100.7"), "auth") == "auth:203.0.113.9"

    def test_bucket_allows_then_blocks(self):
        mw = RateLimitMiddleware(_dummy_asgi_app())
        for _ in range(5):
            ok, _ = mw._allow("auth:9.9.9.9", 5)
            assert ok
        ok, retry_after = mw._allow("auth:9.9.9.9", 5)
        assert not ok and retry_after >= 1

    def test_429_response_shape(self):
        from fastapi.testclient import TestClient

        async def ok_app(scope, receive, send):
            from starlette.responses import JSONResponse
            await JSONResponse({"ok": True})(scope, receive, send)

        mw = RateLimitMiddleware(ok_app)
        client = TestClient(mw)
        # auth 档 5 次/分钟：打满后第 6 次 429
        for _ in range(5):
            r = client.post("/api/v1/auth/login", json={})
            assert r.status_code != 429
        r = client.post("/api/v1/auth/login", json={})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        body = r.json()
        # v1 路径走标准信封
        assert body["code"] == 2004  # AI_RATE_LIMITED
        # 老路径保持 detail 格式
        mw2 = RateLimitMiddleware(ok_app)
        client2 = TestClient(mw2)
        for _ in range(20):
            client2.post("/api/ideas", json={})
        r = client2.post("/api/ideas", json={})
        assert r.status_code == 429
        assert "detail" in r.json()


# ================= 9-4 / 9-5：API Key 与审计（DB） =================

@pytest.fixture()
def db(tmp_path, monkeypatch):
    from backend.auth.database import get_session_factory, reset_engine
    url = f"sqlite:///{tmp_path}/sec.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()
    reset_engine()


def _org(db):
    from backend.models import Organization
    org = Organization(name="安全测试组织", created_by="u_admin")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


class TestApiKeys:
    def test_generate_format_and_hash_only(self, db):
        from backend import models
        from backend.security.api_keys import (
            generate_api_key, hash_key, verify_api_key,
        )
        org = _org(db)
        plaintext, digest = generate_api_key()
        assert plaintext.startswith("lupin_")
        assert len(plaintext) > 40
        assert digest == hash_key(plaintext)
        assert plaintext not in digest  # hash 不可逆出明文

        rec = models.ApiKey(org_id=org.id, name="ci", key_hash=digest,
                            key_prefix=plaintext[:10], scopes=["ai:use"],
                            created_by="u_admin")
        db.add(rec)
        db.commit()

        # 库里没有明文
        stored = db.query(models.ApiKey).first()
        assert stored.key_hash == digest
        assert getattr(stored, "api_key", None) is None

        # 校验通过
        assert verify_api_key(db, plaintext).id == rec.id
        # 错一个字符就失败
        assert verify_api_key(db, plaintext[:-1] + "X") is None
        # 非 lupin_ 前缀直接拒绝
        assert verify_api_key(db, "sk-abcdef") is None

    def test_revoked_and_expired_rejected(self, db):
        from backend import models
        from backend.security.api_keys import generate_api_key, verify_api_key
        org = _org(db)
        p1, d1 = generate_api_key()
        r1 = models.ApiKey(org_id=org.id, name="r", key_hash=d1,
                           key_prefix=p1[:10], scopes=[],
                           created_by="u",
                           revoked_at=datetime.now(timezone.utc))
        p2, d2 = generate_api_key()
        r2 = models.ApiKey(org_id=org.id, name="e", key_hash=d2,
                           key_prefix=p2[:10], scopes=[], created_by="u",
                           expires_at=datetime.now(timezone.utc)
                           - timedelta(seconds=1))
        db.add_all([r1, r2])
        db.commit()
        assert verify_api_key(db, p1) is None   # 已撤销
        assert verify_api_key(db, p2) is None   # 已过期

    def test_touch_updates_last_used(self, db):
        from backend import models
        from backend.security.api_keys import (
            generate_api_key, touch_api_key, verify_api_key,
        )
        org = _org(db)
        p, d = generate_api_key()
        rec = models.ApiKey(org_id=org.id, name="t", key_hash=d,
                            key_prefix=p[:10], scopes=[], created_by="u")
        db.add(rec)
        db.commit()
        assert rec.last_used_at is None
        rec = verify_api_key(db, p)
        touch_api_key(db, rec)
        db.commit()
        assert rec.last_used_at is not None

    def test_me_endpoint(self, db, monkeypatch):
        # 用真实 app 测 X-API-Key 鉴权链路
        from fastapi.testclient import TestClient
        from backend import models
        from backend.app import app as fastapi_app
        from backend.security.api_keys import generate_api_key

        org = _org(db)
        p, d = generate_api_key()
        db.add(models.ApiKey(org_id=org.id, name="integ", key_hash=d,
                             key_prefix=p[:10], scopes=["ai:use"],
                             created_by="u"))
        db.commit()
        # fixture 的 engine 已指向 tmp 库，app 的 get_db 共用同一 engine

        client = TestClient(fastapi_app, raise_server_exceptions=False)
        r = client.get("/api/v1/api-keys/me", headers={"X-API-Key": p})
        assert r.status_code == 200, r.text[:200]
        assert r.json()["data"]["org_id"] == org.id
        r = client.get("/api/v1/api-keys/me",
                       headers={"X-API-Key": "lupin_bogus"})
        assert r.status_code == 401


class TestAudit:
    def test_log_and_query(self, db):
        from backend import models
        from backend.security.audit import log_audit
        org = _org(db)
        log_audit(db, action="api_key.create", org_id=org.id,
                  user_id="u_admin", object_type="api_key",
                  object_id="k1", detail={"name": "ci"}, ip="1.2.3.4")
        log_audit(db, action="work.delete", org_id=org.id,
                  user_id="u_admin", object_type="work", object_id="w1")
        db.commit()
        rows = (db.query(models.AuditEvent)
                .filter(models.AuditEvent.org_id == org.id)
                .order_by(models.AuditEvent.created_at).all())
        assert [r.action for r in rows] == ["api_key.create", "work.delete"]
        assert rows[0].detail_json == {"name": "ci"}
        assert rows[0].ip == "1.2.3.4"
        # 敏感字段不应出现在 detail（约定：调用方不传 Key 明文）
        assert "api_key" not in str(rows[0].detail_json)

    def test_log_audit_never_raises(self, db):
        from backend.security.audit import log_audit
        # 传一个坏掉的 session 也不能抛异常破坏主流程
        class BadSession:
            def add(self, *a, **k):
                raise RuntimeError("db down")

            def flush(self):
                raise RuntimeError("db down")
        log_audit(BadSession(), action="x")  # 不抛


# ================= 9-6：上传校验 =================

class TestUpload:
    def test_sniff_images(self):
        assert sniff_kind(b"\x89PNG\r\n\x1a\nrest") == "image"
        assert sniff_kind(b"\xff\xd8\xff\xe0rest") == "image"
        assert sniff_kind(b"GIF89arest") == "image"
        assert sniff_kind(b"RIFF\x00\x00\x00\x00WEBPrest") == "image"
        # RIFF 但不是 WEBP → 不认
        assert sniff_kind(b"RIFF\x00\x00\x00\x00AVI rest") is None

    def test_sniff_video_archive(self):
        assert sniff_kind(b"\x00\x00\x00\x18ftypmp42rest") == "video"
        assert sniff_kind(b"\x1a\x45\xdf\xa3rest") == "video"
        assert sniff_kind(b"PK\x03\x04rest") == "archive"
        assert sniff_kind(b"\x1f\x8brest") == "archive"

    def test_unknown_rejected(self):
        assert sniff_kind(b"MZ\x90\x00exe") is None  # 可执行文件
        assert sniff_kind(b"") is None
        assert sniff_kind(b"hello world") is None
        with pytest.raises(UploadRejected):
            validate_upload("evil.exe", b"MZ\x90\x00", 100)

    def test_size_tiers(self):
        png = b"\x89PNG\r\n\x1a\n"
        assert validate_upload("a.png", png, 1024) == "image"
        with pytest.raises(UploadRejected):
            validate_upload("big.png", png, SIZE_LIMITS["image"] + 1)
        mp4 = b"\x00\x00\x00\x18ftyp"
        assert validate_upload("v.mp4", mp4, SIZE_LIMITS["video"]) == "video"
        with pytest.raises(UploadRejected):
            validate_upload("v.mp4", mp4, SIZE_LIMITS["video"] + 1)

    def test_extension_not_trusted(self):
        # 扩展名是 png 但魔数是 exe → 拒绝（不认扩展名）
        with pytest.raises(UploadRejected):
            validate_upload("photo.png", b"MZ\x90\x00fake", 100)
