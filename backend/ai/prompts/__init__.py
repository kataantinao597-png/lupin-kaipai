"""141-160：Prompt 版本化。

每个任务一个版本文件，含 VERSION 常量。网关调用时记录 prompt_version
到 ai_tasks.prompt_version，可精确追溯每次调用用的哪版 prompt。

版本规则（强制）：
1. 已发布版本文件禁止直接修改；改动 → 新建 <task>_v<N+1>.py。
2. 新版本在下方 registry 中替换旧模块。
3. common.py 公共块的 breaking 变更同样走新版本。

task_type → prompt 版本映射：
"""
from . import feasibility_v1, ideas_v1, regen_shot_v1, risk_review_v1, storyboard_v1
from .common import FACT_GROUNDING_RULES, SHOT_FIELDS_DESC, facts_block, product_block

PROMPT_MODULES = {
    "ideas": ideas_v1,
    "storyboard": storyboard_v1,
    "regen_shot": regen_shot_v1,
    "feasibility": feasibility_v1,
    "risk_check": risk_review_v1,
}

# 兼容：老 backend/prompts.py 的导入路径
from .feasibility_v1 import build_feasibility_messages
from .ideas_v1 import build_idea_messages
from .regen_shot_v1 import build_regen_shot_messages
from .risk_review_v1 import REVIEW_PROMPT_TMPL, build_risk_review_messages
from .storyboard_v1 import build_storyboard_messages


def get_prompt_version(task_type: str) -> str:
    """取某任务当前 prompt 版本；未知任务返回 "unknown"。"""
    mod = PROMPT_MODULES.get(task_type)
    return mod.VERSION if mod is not None else "unknown"


def get_prompt_module(task_type: str):
    """取某任务的 prompt 模块（评估框架用）。"""
    return PROMPT_MODULES.get(task_type)


__all__ = [
    "PROMPT_MODULES",
    "get_prompt_version",
    "get_prompt_module",
    "build_idea_messages",
    "build_storyboard_messages",
    "build_regen_shot_messages",
    "build_feasibility_messages",
    "build_risk_review_messages",
    "REVIEW_PROMPT_TMPL",
    "FACT_GROUNDING_RULES",
    "SHOT_FIELDS_DESC",
    "facts_block",
    "product_block",
]
