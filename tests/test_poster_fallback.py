"""海报生图备选通道：主 luna 超时/429/5xx/连接失败时自动切备选。

- _call_image_api：主成功不走备选；主可重试失败→备选成功；备选也失败→报错；
  400/401 不切备选；无备选 key 时主失败直接报错；无主 key 时 503。
- mock _call_image_api_once，不调真实服务。

业务库 backend/data/lupin.db 只读，绝不写入/迁移。

运行：./venv/bin/python -m pytest tests/test_poster_fallback.py
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.core.exceptions import BusinessError  # noqa: E402
from backend.api.v1 import poster_image as pi  # noqa: E402


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


class TestFallbackConf:
    def test_fallback_conf_defaults(self, monkeypatch):
        _env(monkeypatch,
             LUPIN_IMAGE_API_BASE="https://deepkey.top/v1",
             LUPIN_IMAGE_API_KEY="primary-key",
             LUPIN_IMAGE_MODEL="gpt-6-luna",
             LUPIN_IMAGE_API_BASE_FALLBACK=None,
             LUPIN_IMAGE_API_KEY_FALLBACK=None,
             LUPIN_IMAGE_MODEL_FALLBACK=None)
        base, key, model = pi._image_fallback_conf()
        assert base == "https://deepkey.top/v1"
        assert key == ""
        assert model == "gpt-image-2"

    def test_fallback_conf_explicit(self, monkeypatch):
        _env(monkeypatch,
             LUPIN_IMAGE_API_BASE="https://deepkey.top/v1",
             LUPIN_IMAGE_API_KEY="primary-key",
             LUPIN_IMAGE_API_BASE_FALLBACK="https://api.openai.com",
             LUPIN_IMAGE_API_KEY_FALLBACK="fb-key",
             LUPIN_IMAGE_MODEL_FALLBACK="gpt-image-2.5-flare")
        base, key, model = pi._image_fallback_conf()
        assert base == "https://api.openai.com"
        assert key == "fb-key"
        assert model == "gpt-image-2.5-flare"


class TestAutoFallback:
    def _keys(self, monkeypatch):
        _env(monkeypatch,
             LUPIN_IMAGE_API_BASE="https://deepkey.top/v1",
             LUPIN_IMAGE_API_KEY="primary-key",
             LUPIN_IMAGE_MODEL="gpt-6-luna",
             LUPIN_IMAGE_API_KEY_FALLBACK="fb-key",
             LUPIN_IMAGE_MODEL_FALLBACK="gpt-image-2")

    def test_primary_ok_no_fallback(self, monkeypatch):
        self._keys(monkeypatch)
        calls = []

        def fake_once(base, key, model, prompt, size):
            calls.append(key)
            return {"b64_json": "primary-img"}
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        out = pi._call_image_api("临沂炒鸡", "1024x1024")
        assert out["b64_json"] == "primary-img"
        assert calls == ["primary-key"], "主成功不应调备选"

    def test_primary_429_falls_back(self, monkeypatch):
        self._keys(monkeypatch)
        calls = []

        def fake_once(base, key, model, prompt, size):
            calls.append(key)
            if key == "primary-key":
                raise pi._RetryableUpstreamError("图片服务返回 429")
            return {"b64_json": "fallback-img"}
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        out = pi._call_image_api("临沂炒鸡", "1024x1024")
        assert out["b64_json"] == "fallback-img"
        assert calls == ["primary-key", "fb-key"], "主 429 应自动切备选"

    def test_primary_timeout_falls_back(self, monkeypatch):
        self._keys(monkeypatch)

        def fake_once(base, key, model, prompt, size):
            if key == "primary-key":
                raise pi._RetryableUpstreamError(
                    "图片服务调用失败：TimeoutException")
            return {"b64_json": "fallback-img"}
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        out = pi._call_image_api("临沂炒鸡", "1024x1024")
        assert out["b64_json"] == "fallback-img"

    def test_both_fail_raises(self, monkeypatch):
        self._keys(monkeypatch)

        def fake_once(base, key, model, prompt, size):
            raise pi._RetryableUpstreamError("图片服务返回 500")
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        with pytest.raises(BusinessError) as ei:
            pi._call_image_api("临沂炒鸡", "1024x1024")
        assert "主备通道均失败" in str(ei.value)
        assert ei.value.http_status == 502

    def test_primary_400_no_fallback(self, monkeypatch):
        """400（提示词等问题）不切备选，直接报错。"""
        self._keys(monkeypatch)
        calls = []

        def fake_once(base, key, model, prompt, size):
            calls.append(key)
            raise BusinessError("图片服务返回 400，请检查配置",
                                code="AI_PROVIDER_ERROR", http_status=502)
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        with pytest.raises(BusinessError):
            pi._call_image_api("临沂炒鸡", "1024x1024")
        assert calls == ["primary-key"], "400 不应切备选"

    def test_primary_401_no_fallback(self, monkeypatch):
        """401（key 配置错误）不切备选——配置错误不应被掩盖。"""
        self._keys(monkeypatch)
        calls = []

        def fake_once(base, key, model, prompt, size):
            calls.append(key)
            raise BusinessError("图片服务返回 401，请检查配置",
                                code="AI_PROVIDER_ERROR", http_status=502)
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        with pytest.raises(BusinessError):
            pi._call_image_api("临沂炒鸡", "1024x1024")
        assert calls == ["primary-key"], "401 不应切备选"

    def test_no_fallback_key_primary_fail_raises(self, monkeypatch):
        """没配备选 key：主失败直接报错。"""
        _env(monkeypatch,
             LUPIN_IMAGE_API_KEY="primary-key",
             LUPIN_IMAGE_MODEL="gpt-6-luna",
             LUPIN_IMAGE_API_KEY_FALLBACK=None)

        def fake_once(base, key, model, prompt, size):
            raise pi._RetryableUpstreamError("图片服务返回 429")
        monkeypatch.setattr(pi, "_call_image_api_once", fake_once)
        with pytest.raises(BusinessError) as ei:
            pi._call_image_api("临沂炒鸡", "1024x1024")
        assert ei.value.http_status == 502

    def test_no_primary_key_503(self, monkeypatch):
        _env(monkeypatch,
             LUPIN_IMAGE_API_KEY=None,
             LUPIN_IMAGE_API_KEY_FALLBACK="fb-key")
        with pytest.raises(BusinessError) as ei:
            pi._call_image_api("临沂炒鸡", "1024x1024")
        assert ei.value.http_status == 503

    def test_status_reports_fallback(self, monkeypatch):
        _env(monkeypatch,
             LUPIN_IMAGE_API_KEY="primary-key",
             LUPIN_IMAGE_API_KEY_FALLBACK="fb-key")

        class FakeUser:
            id = "u1"
        out = pi.image_status(FakeUser())
        assert out["data"]["configured"] is True
        assert out["data"]["fallback_configured"] is True

        _env(monkeypatch, LUPIN_IMAGE_API_KEY_FALLBACK=None)
        out = pi.image_status(FakeUser())
        assert out["data"]["fallback_configured"] is False
