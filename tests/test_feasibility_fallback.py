import pytest

pytestmark = pytest.mark.unit

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from starlette.requests import Request

# B-14（测试陈旧）：旧 app.get_ai / app.check_feasibility 已随路由拆分
# 迁到 backend/api/v1/feasibility.py；模型失败降级逻辑未变（AIFatalError
# → 纯规则结果、model_completed=False、不抛 502）。新缝合点是 gw 参数
# （原先 patch app.get_ai 的位置），测试直接注入失败网关。
from backend.api.v1.feasibility import FeasibilityCheckRequest, check_feasibility
from backend.ai_service import AIFatalError


def request_for_test():
    return Request({"type": "http", "method": "POST", "path": "/api/feasibility-check", "headers": [], "client": ("test", 1234), "query_string": b""})


def valid_storyboard():
    return {
        "product_summary": "测试产品",
        "target_audience": "消费者",
        "creative_direction": "真实展示",
        "target_duration_seconds": 5,
        "shoot": {"device": "手机", "people": 1, "lighting": "自然光", "location": "厨房"},
        "shots": [{
            "shot_number": 1, "duration_seconds": 5, "shot_type": "近景",
            "scene_description": "展示产品", "shooting_action": "手持手机拍摄",
            "voiceover": "这是测试产品", "subtitle": "测试产品",
            "props": [], "required_assets": [], "camera_movement": "固定",
            "evidence_notes": "无", "fact_refs": [],
        }],
        "materials_checklist": [], "risk_flags": [], "pending_confirmations": [],
    }


class FeasibilityFallbackTests(unittest.TestCase):
    def test_model_failure_returns_rule_results_without_502(self):
        class FailingGW:
            def generate_with_retry(self, *args, **kwargs):
                raise AIFatalError("模型不可用")

        result = check_feasibility(
            FeasibilityCheckRequest(storyboard=valid_storyboard()),
            request_for_test(),
            gw=FailingGW(),
            user=None,
            db=None,
        )
        self.assertFalse(result["model_completed"])
        self.assertEqual(result["results"][0]["status"], "需要人工确认")
        self.assertIn("模型检查未完成", result["notice"])


if __name__ == "__main__":
    unittest.main()
