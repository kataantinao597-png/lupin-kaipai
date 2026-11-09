import pytest

pytestmark = pytest.mark.unit

import inspect
import json
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import app as app_module
from backend.ai_service import AIService, AIFatalError, parse_json_content


class HandoverRegressionTests(unittest.TestCase):
    def test_parse_json_content_accepts_provider_wrappers(self):
        expected = {"ideas": []}
        self.assertEqual(parse_json_content('{"ideas": []}'), expected)
        self.assertEqual(parse_json_content('```json\n{"ideas": []}\n```'), expected)
        self.assertEqual(parse_json_content('Here is the JSON:\n{"ideas": []}'), expected)
        self.assertEqual(parse_json_content([{"type": "text", "text": '{"ideas": []}'}]), expected)

    def test_parse_json_content_rejects_non_object_json(self):
        with self.assertRaises(ValueError):
            parse_json_content('[1, 2, 3]')

    def test_generate_with_retry_accepts_wrapped_json_without_retrying(self):
        service = AIService(api_key="test-key", base_url="https://example.com/v1")
        calls = []

        def fake_chat(messages):
            calls.append(messages)
            return "```json\n{\"ok\": true}\n```", {"total_tokens": 1}

        result, meta = service.generate_with_retry(
            lambda feedback: [{"role": "user", "content": feedback or "go"}],
            lambda data: ([], data) if data.get("ok") else (["missing ok"], None),
            chat_fn=fake_chat,
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(meta["attempts"], 1)
        self.assertEqual(len(calls), 1)

    def test_regenerate_shot_declares_request_context(self):
        """The route must receive Request before using rate limiting or headers."""
        request = inspect.signature(app_module.regenerate_shot).parameters
        self.assertIn("request", request)
        self.assertIs(request["request"].annotation, app_module.Request)

    def test_health_reports_configured_model(self):
        old_ai = app_module.ai
        try:
            class FakeAI:
                call_count = 0
                total_tokens = 0
                model = "custom-model"

            app_module.ai = FakeAI()
            self.assertEqual(app_module.health()["model"], "custom-model")
        finally:
            app_module.ai = old_ai

    def test_non_json_success_response_is_reported_as_fatal_error(self):
        service = AIService(api_key="test-key", base_url="https://example.com/v1")

        class Response:
            status_code = 200
            text = "<html>not json</html>"

            def json(self):
                raise ValueError("invalid json")

        # 轻微 #5：_post 现为每次调用新建独立 Session（DeepSeek 复查出的
        # 线程安全修复），故 mock 点从实例 session 改为 Session 类本身；
        # 断言意图不变：HTTP 200 但 body 非 JSON → AIFatalError 且文案含 JSON。
        with patch("backend.ai_service.requests.Session") as MockSession:
            MockSession.return_value.__enter__.return_value.post.return_value = Response()
            with self.assertRaises(AIFatalError) as ctx:
                service.chat([{"role": "user", "content": "hi"}])
        self.assertIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
