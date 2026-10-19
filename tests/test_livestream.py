"""店播场控脚本（Top10 #7）测试。

- 提示词模板：build_livestream_messages 结构/品类/时长
- 注册：PROMPT_MODULES / TASK_REQUIREMENTS / TASK_TYPES / RUNNERS
- 校验器：_validate_livestream
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402


PRODUCT = {
    "product_name": "烟台苹果",
    "description": "山东烟台出产的红富士苹果",
    "selling_points": ["脆甜多汁", "果园现摘现发"],
    "price": "9.9元/斤",
    "audience": "家庭用户",
    "shoot": {"device": "手机", "people": 2, "lighting": "自然光",
              "location": "果园", "target_duration_seconds": 60},
}


class TestPrompt:
    def test_build_messages(self):
        from backend.ai.prompts.livestream_v1 import build_livestream_messages
        msgs = build_livestream_messages(PRODUCT, [], "水果", 120)
        assert isinstance(msgs, list) and len(msgs) >= 1
        content = msgs[0]["content"]
        assert "120" in content and "水果" in content
        assert "烟台苹果" in content
        assert "只输出 JSON" in content

    def test_category_hint(self):
        from backend.ai.prompts.livestream_v1 import (
            build_livestream_messages, CATEGORY_HINTS)
        assert "水果" in CATEGORY_HINTS and "蔬菜" in CATEGORY_HINTS
        msgs = build_livestream_messages(PRODUCT, [], "蔬菜", 60)
        assert "60" in msgs[0]["content"]

    def test_version_registry(self):
        from backend.ai.prompts import get_prompt_version, get_prompt_module
        assert get_prompt_version("livestream_script") == "livestream_v1"
        assert get_prompt_module("livestream_script") is not None


class TestRegistration:
    def test_capabilities(self):
        from backend.ai.capabilities import TASK_REQUIREMENTS
        req = TASK_REQUIREMENTS.get("livestream_script")
        assert req is not None and req.get("needs_json") is True

    def test_task_types(self):
        from backend.models import TASK_TYPES
        assert "livestream_script" in TASK_TYPES

    def test_runner_registered(self):
        from backend.ai_tasks.runners import RUNNERS, run_livestream_script
        assert RUNNERS.get("livestream_script") is run_livestream_script

    def test_ai_task_types(self):
        from backend.models import AI_TASK_TYPES
        assert "livestream_script" in AI_TASK_TYPES


class TestValidator:
    def test_valid(self):
        from backend.ai_tasks.runners import _validate_livestream
        data = {
            "title": "果园现摘烟台苹果专场",
            "total_minutes": 120,
            "segments": [
                {"phase": "开场留人", "start_minute": 0, "end_minute": 5,
                 "goal": "留人", "script": ["欢迎来到直播间"],
                 "tips": ["发福袋"]},
                {"phase": "讲品", "start_minute": 5, "end_minute": 120,
                 "goal": "讲透产品", "script": ["看这个果"],
                 "tips": []},
            ],
            "lucky_bag_points": [],
            "closing_scripts": ["最后 10 单"],
            "rescue_scripts": [],
            "prep_checklist": [],
        }
        errs, out = _validate_livestream(data)
        assert errs == [] and out is data

    def test_invalid(self):
        from backend.ai_tasks.runners import _validate_livestream
        errs, out = _validate_livestream({"segments": []})
        assert errs and out is None
        errs, out = _validate_livestream({"segments": [{"phase": "讲品"}]})
        assert any("缺少字段" in e for e in errs)
        errs, out = _validate_livestream("not-a-dict")
        assert errs and out is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
