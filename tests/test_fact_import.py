"""产品文档导入测试（待接入 #1）。

- 规则抽取：markdown 清洗、长度过滤、去重、分类猜测
- API：/api/v1/facts/import-document（.txt/.md 解析、非法格式拒绝、AI 降级）
- API：/api/v1/facts/batch（批量写入、权限隔离）
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402

from backend.api.v1.fact_import import (  # noqa: E402
    extract_rule_candidates, guess_category,
)


# ================= 规则抽取单元测试 =================

class TestRuleExtraction:
    def test_basic_lines(self):
        text = "烟台苹果产自山东烟台。\n果径 80mm 以上。\n脆甜多汁，果园现摘现发。"
        cands = extract_rule_candidates(text)
        assert len(cands) == 3
        assert all(c["source"] == "rule" for c in cands)
        assert cands[0]["category"] == "产地"
        assert cands[1]["category"] == "规格"
        assert cands[2]["category"] == "口感"

    def test_markdown_stripped(self):
        text = "# 烟台苹果\n\n![图](http://x/y.jpg)\n**脆甜多汁**的口感让人难忘\n\n```\ncode block\n```\n"
        cands = extract_rule_candidates(text)
        contents = [c["content"] for c in cands]
        assert not any("http" in c or "code block" in c for c in contents)
        assert any("脆甜多汁" in c for c in contents)

    def test_length_filter_and_dedupe(self):
        text = "短\n" + "烟台苹果产自山东烟台。" * 2 + "\n" + "x" * 300
        cands = extract_rule_candidates(text)
        assert len(cands) == 1  # 短行、超长行、重复行都被过滤

    def test_guess_category_default(self):
        assert guess_category("今天天气不错") == "其他"
        assert guess_category("传统卤制工艺") == "工艺"
        assert guess_category("通过绿色食品认证") == "认证"
        assert guess_category("冷藏保存") == "保存方式"


# ================= API 测试 =================

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：user=owner。"""
    url = f"sqlite:///{tmp_path}/factimport.db"
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
    command.upgrade(cfg, "heads")

    from backend.models import Organization, Membership, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="导入测试社", created_by="u_owner")
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


def _no_ai_gw():
    """AI 网关覆盖：直接抛配置错误，触发规则降级。"""
    from backend.ai_service import AIConfigError
    raise AIConfigError("未配置 Key（测试）")


class TestImportDocumentAPI:
    def test_txt_rule_fallback(self, api_client, monkeypatch):
        import backend.api.v1.fact_import as fi
        client, org_id, _ = api_client
        monkeypatch.setattr(fi, "_optional_gateway", lambda req: None)
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": org_id, "product_id": "烟台苹果"},
            files={"file": ("doc.txt", "烟台苹果产自山东烟台。\n脆甜多汁，果园现摘现发。".encode("utf-8"))},
        )
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["ai_used"] is False
        assert len(d["candidates"]) == 2
        assert d["candidates"][0]["category"] == "产地"

    def test_md_ok(self, api_client):
        client, org_id, _ = api_client
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": org_id},
            files={"file": ("doc.md", "# 章丘大葱\n\n葱白长达60厘米。".encode("utf-8"))},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]["candidates"]) >= 1

    def test_ai_path(self, api_client, monkeypatch):
        """AI 成功时 ai_used=true，候选来自 AI。"""
        import backend.api.v1.fact_import as fi
        client, org_id, _ = api_client

        class FakeGW:
            def generate_with_retry(self, task_type, make_messages,
                                    validate_fn, **kw):
                assert task_type == "fact_extract"
                data = {"candidates": [
                    {"content": "德州扒鸡源自山东德州", "category": "产地"},
                    {"content": "这是一条凑字数的测试内容", "category": "不存在的分类"},
                ]}
                errs, out = validate_fn(data)
                assert errs == []
                return out, {}

        monkeypatch.setattr(fi, "_optional_gateway",
                            lambda req: FakeGW())
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": org_id},
            files={"file": ("doc.txt", "随便写点内容凑数用。".encode("utf-8"))},
        )
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["ai_used"] is True
        assert d["candidates"][0] == {
            "content": "德州扒鸡源自山东德州", "category": "产地",
            "source": "ai"}
        # 非法分类被归一化为"其他"
        assert d["candidates"][1]["category"] == "其他"

    def test_reject_bad_ext(self, api_client):
        client, org_id, _ = api_client
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": org_id},
            files={"file": ("doc.pdf", b"%PDF-1.4 fake")},
        )
        assert r.status_code == 400

    def test_reject_empty(self, api_client):
        client, org_id, _ = api_client
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": org_id},
            files={"file": ("doc.txt", b"   \n  ")},
        )
        assert r.status_code == 400

    def test_unauthorized(self, api_client):
        from backend.app import app
        from backend.auth.deps import get_current_user
        client, org_id, _ = api_client
        app.dependency_overrides.pop(get_current_user, None)
        try:
            r = client.post(
                "/api/v1/facts/import-document",
                data={"org_id": org_id},
                files={"file": ("doc.txt", "内容".encode("utf-8"))},
            )
        finally:
            pass
        assert r.status_code in (401, 403)

    def test_wrong_org(self, api_client):
        client, _org_id, _ = api_client
        r = client.post(
            "/api/v1/facts/import-document",
            data={"org_id": "no-such-org"},
            files={"file": ("doc.txt", "烟台苹果产自山东。".encode("utf-8"))},
        )
        assert r.status_code == 404


class TestBatchAPI:
    def test_batch_create(self, api_client):
        client, org_id, _ = api_client
        r = client.post("/api/v1/facts/batch", json={
            "org_id": org_id, "product_id": "烟台苹果",
            "facts": [
                {"content": "烟台苹果产自山东烟台", "category": "产地",
                 "source_type": "文档导入", "source_ref": "doc.txt"},
                {"content": "脆甜多汁", "category": "口感"},
            ],
        })
        assert r.status_code == 201, r.text
        items = r.json()["data"]
        assert len(items) == 2
        assert items[0]["status"] == "draft"
        assert items[0]["source_type"] == "文档导入"
        # 列表可查到
        r2 = client.get("/api/v1/facts", params={"org_id": org_id})
        assert r2.status_code == 200
        assert len(r2.json()["data"]) == 2

    def test_batch_reject_empty(self, api_client):
        client, org_id, _ = api_client
        r = client.post("/api/v1/facts/batch",
                        json={"org_id": org_id, "facts": []})
        assert r.status_code == 422

    def test_batch_wrong_org(self, api_client):
        client, _org_id, _ = api_client
        r = client.post("/api/v1/facts/batch", json={
            "org_id": "no-such-org",
            "facts": [{"content": "x" * 10, "category": "其他"}],
        })
        assert r.status_code == 404
