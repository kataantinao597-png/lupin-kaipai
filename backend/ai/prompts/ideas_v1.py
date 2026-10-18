"""ideas prompt v1。

版本规则：任何改动都必须新建文件（如 ideas_v2.py）并更新 registry，
禁止直接修改已发布的版本文件——已生成的 ai_tasks.prompt_version
必须能精确追溯到当时的 prompt 文本。
"""
from typing import List

from .common import FACT_GROUNDING_RULES, facts_block, product_block

VERSION = "ideas_v1"


def build_idea_messages(p: dict, facts: list = None) -> List[dict]:
    prompt = f"""你是一位短视频创意策划师，擅长为山东农产品商家策划抖音/快手带货短视频。

{product_block(p)}

已确认的产品事实：
{facts_block(facts)}
{FACT_GROUNDING_RULES}
请给出3个明确不同的创意方向。3个方向必须有明显差异（比如情感路线/测评路线/场景路线）。

只输出 JSON（不要任何解释文字）：
{{"ideas": [{{"name": "方向名称", "tagline": "一句话创意点", "hook_3s": "开场3秒设计",
"core_idea": "核心思路", "audience_fit": "为什么适合目标受众",
"difficulty": "拍摄难度（低/中/高）", "materials_needed": "素材需求",
"fact_refs": ["依据的事实ID"]}}]}}
"""
    return [{"role": "user", "content": prompt}]
