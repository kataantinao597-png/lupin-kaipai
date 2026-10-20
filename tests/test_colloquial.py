"""农人口语改写器（Top10 #9）测试。

- 提示词模板：build_colloquial_messages 结构/约束
- 注册：PROMPT_MODULES / TASK_REQUIREMENTS
- 校验器：_validate_colloquial
- API：POST /api/colloquial-rewrite（无 AI 配置时 503 诚实失败，不伪造）
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402


class TestPrompt:
    def test_build_messages(self):
        from backend.ai.prompts.colloquial_v1 import build_colloquial_messages
        msgs = build_colloquial_messages("这款苹果口感脆甜，欢迎大家购买。", "烟台苹果")
        assert isinstance(msgs, list) and len(msgs) == 1
        content = msgs[0]["content"]
        assert "这款苹果口感脆甜" in content
        assert "烟台苹果" in content
        assert "rewritten" in content
        assert "只输出 JSON" in content

    def test_constraints_present(self):
        from backend.ai.prompts.colloquial_v1 import build_colloquial_messages
        content = build_colloquial_messages("测试文案")[0]["content"]
        # 关键约束必须在提示词里：不编造方言、不改变原意、不加功效宣称
        assert "方言" in content
        assert "原意" in content
        assert "功效" in content

    def test_version_registry(self):
        from backend.ai.prompts import get_prompt_version, get_prompt_module
        # V3.1：改写提示词升级到 v2（注入已确认事实做 grounding）
        assert get_prompt_version("colloquial_rewrite") == "colloquial_v2"
        assert get_prompt_module("colloquial_rewrite") is not None


class TestRegistration:
    def test_capabilities(self):
        from backend.ai.capabilities import TASK_REQUIREMENTS
        req = TASK_REQUIREMENTS.get("colloquial_rewrite")
        assert req is not None and req.get("needs_json") is True


class TestValidator:
    def test_valid(self):
        from backend.api.v1.colloquial import _validate_colloquial
        errs, out = _validate_colloquial(
            {"rewritten": "恁看这苹果，脆甜！", "changes": ["改口语了"]})
        assert errs == []

    def test_invalid(self):
        from backend.api.v1.colloquial import _validate_colloquial
        errs, out = _validate_colloquial({"changes": []})
        assert errs and out is None
        errs, out = _validate_colloquial({"rewritten": "   "})
        assert errs and out is None


# ================= API 测试（无 AI 时 503） =================

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/colloquial.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
    # 确保无 AI 配置：清掉可能残留的 key 与单例缓存
    # 置空串而非 delenv：pydantic-settings 的 env_file=".env" 是直接读文件的，
    # 不经过 os.environ，删掉环境变量后仍会被 .env 里的真实 Key 填上。
    # 显式空串优先级高于 env_file，才能稳定表达"未配置 AI"。
    monkeypatch.setenv("LUPIN_API_KEY", "")
    import backend.auth  # noqa: F401
    from backend.core import deps as core_deps
    if hasattr(core_deps.get_ai_service, "_singleton"):
        delattr(core_deps.get_ai_service, "_singleton")
    from backend.auth.database import reset_engine, get_session_factory
    from backend.media import reset_storage
    from backend.media.signing import reset_sign_secret
    reset_engine()
    reset_storage()
    reset_sign_secret()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "heads")

    from fastapi.testclient import TestClient
    from backend.app import app

    # V3.1 P0-2：接口改为强制登录。这些用例只关心"无 AI 配置时的诚实失败"，
    # 因此直接覆盖鉴权依赖，避免为了鉴权再搭一套登录/组织数据。
    from backend.auth import deps as auth_deps
    from backend.auth.models import User
    _stub = User(id="u-colloquial-test", email="colloquial@test.local",
                 password_hash="x", display_name="测试", is_active=True)
    app.dependency_overrides[auth_deps.get_current_user] = lambda: _stub

    client = TestClient(app, raise_server_exceptions=False)
    yield client
    app.dependency_overrides.clear()


class TestAPI:
    def test_no_ai_503(self, api_client):
        r = api_client.post("/api/colloquial-rewrite",
                            json={"text": "这款苹果口感脆甜，欢迎购买。"})
        assert r.status_code == 503
        # 诚实失败：不返回伪造的改写内容
        assert "rewritten" not in r.json()

    def test_validation_400(self, api_client):
        # 空文本：pydantic 422 或无 AI 时 503（依赖先于 body 校验），都不伪造内容
        r = api_client.post("/api/colloquial-rewrite", json={"text": ""})
        assert r.status_code in (422, 503)
        assert "rewritten" not in r.json()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
