"""评估用例：固定输入 + 期望 JSON schema + 检查点。

用例使用版本化 prompt 构建输入，保证评估的是当前线上 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class EvalCase:
    """单个评估用例。"""
    name: str
    task_type: str                      # ideas / storyboard / risk_check ...
    messages: List[dict]                # 固定输入
    expected_schema: dict               # 期望 JSON schema（最小子集）
    checkpoints: List[str] = field(default_factory=list)  # 人工检查点描述
    prompt_version: str = ""


def _demo_product() -> dict:
    return {
        "product_name": "沂源红苹果",
        "description": "山东沂源出产的红富士苹果，脆甜多汁",
        "selling_points": ["脆甜", "现摘现发"],
        "price": "59元/5斤",
        "audience": "25-40岁家庭主妇",
        "shoot": {"device": "手机", "people": "1", "lighting": "自然光",
                  "location": "果园", "target_duration_seconds": 30},
    }


def _demo_facts() -> list:
    return [{"id": "f1", "content": "沂源县是苹果优生区",
             "category": "产地", "source": "用户提供",
             "status": "confirmed", "allow_use": True}]


def build_fixed_cases() -> List[EvalCase]:
    """构造固定用例集。"""
    from ..prompts import (
        build_feasibility_messages,
        build_idea_messages,
        build_risk_review_messages,
        build_storyboard_messages,
        get_prompt_version,
    )
    product = _demo_product()
    facts = _demo_facts()
    return [
        EvalCase(
            name="ideas_basic",
            task_type="ideas",
            messages=build_idea_messages(product, facts),
            expected_schema={"type": "object", "required": ["ideas"],
                             "properties": {"ideas": {"type": "array"}}},
            checkpoints=["3个创意方向差异明显", "fact_refs 只引用已确认事实"],
            prompt_version=get_prompt_version("ideas"),
        ),
        EvalCase(
            name="storyboard_basic",
            task_type="storyboard",
            messages=build_storyboard_messages(product, "情感路线", facts),
            expected_schema={"type": "object",
                             "required": ["shots", "target_duration_seconds"],
                             "properties": {"shots": {"type": "array"}}},
            checkpoints=["镜头时长总和≈目标时长", "无航拍等多机位要求"],
            prompt_version=get_prompt_version("storyboard"),
        ),
        EvalCase(
            name="risk_check_basic",
            task_type="risk_check",
            messages=build_risk_review_messages(
                ["沂源苹果全国销量第一", "现摘现发很新鲜"],
                evidence="", product_info="沂源红苹果"),
            expected_schema={"type": "object", "required": ["flags"],
                             "properties": {"flags": {"type": "array"}}},
            checkpoints=["'全国销量第一'被标为风险", "不标记'现摘现发'"],
            prompt_version=get_prompt_version("risk_check"),
        ),
        EvalCase(
            name="feasibility_basic",
            task_type="feasibility",
            messages=build_feasibility_messages({
                "target_duration_seconds": 30,
                "shoot": {"device": "手机", "people": "1"},
                "shots": [{"shot_number": 1, "duration_seconds": 30,
                           "shot_type": "特写",
                           "scene_description": "手持苹果特写",
                           "shooting_action": "单手拿苹果展示",
                           "camera_movement": "固定机位",
                           "props": [], "required_assets": []}],
            }),
            expected_schema={"type": "object", "required": ["results"],
                             "properties": {"results": {"type": "array"}}},
            checkpoints=["单人手机可执行判为可执行"],
            prompt_version=get_prompt_version("feasibility"),
        ),
    ]


FIXED_CASES: List[EvalCase] = build_fixed_cases()
