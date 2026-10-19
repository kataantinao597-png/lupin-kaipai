import pytest

pytestmark = pytest.mark.unit

import inspect
import json
import os
import sys
import typing
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi import Request

from backend.api.v1 import ideas as ideas_module
from backend.app import health
from backend.core import deps as _deps
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
        sig = inspect.signature(ideas_module.regenerate_shot)
        self.assertIn("request", sig.parameters)
        # ideas.py 使用 from __future__ import annotations，signature 给出的是
        # 字符串注解，用 get_type_hints 解析后再与 fastapi.Request 对比。
        hints = typing.get_type_hints(ideas_module.regenerate_shot)
        self.assertIs(hints["request"], Request)

    def test_health_reports_configured_model(self):
        sentinel = object()
        old = getattr(_deps.get_ai_service, "_singleton", sentinel)
        try:
            class FakeAI:
                call_count = 0
                total_tokens = 0
                model = "custom-model"

            _deps.get_ai_service._singleton = FakeAI()
            self.assertEqual(health()["model"], "custom-model")
        finally:
            if old is sentinel:
                try:
                    delattr(_deps.get_ai_service, "_singleton")
                except AttributeError:
                    pass
            else:
                _deps.get_ai_service._singleton = old

    def test_non_json_success_response_is_reported_as_fatal_error(self):
        service = AIService(api_key="test-key", base_url="https://example.com/v1")

        class Response:
            status_code = 200
            text = "<html>not json</html>"

            def json(self):
                raise ValueError("invalid json")

        with patch.object(service.session, "post", return_value=Response()):
            with self.assertRaises(AIFatalError) as ctx:
                service.chat([{"role": "user", "content": "hi"}])
        self.assertIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
