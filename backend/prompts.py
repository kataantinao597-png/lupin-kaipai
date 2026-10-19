"""鲁品开拍 - 提示词构造（兼容层）。

实际 prompt 已版本化，见 backend/ai/prompts/（141-160）。
此模块保留老导入路径，全部 re-export 自版本化包。
"""
from .ai.prompts import (
    FACT_GROUNDING_RULES,
    SHOT_FIELDS_DESC,
    build_feasibility_messages,
    build_idea_messages,
    build_regen_shot_messages,
    build_storyboard_messages,
    build_storyboard_v2_messages,
    facts_block,
    get_prompt_module,
    get_prompt_version,
    product_block,
)

__all__ = [
    "SHOT_FIELDS_DESC",
    "facts_block",
    "FACT_GROUNDING_RULES",
    "product_block",
    "build_idea_messages",
    "build_storyboard_messages",
    "build_storyboard_v2_messages",
    "build_regen_shot_messages",
    "build_feasibility_messages",
    "get_prompt_version",
    "get_prompt_module",
]
