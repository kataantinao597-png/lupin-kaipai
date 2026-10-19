"""plan prompt v1：内容策划工作台（V3.0 C）。

版本规则：任何改动都必须新建文件（如 plan_v2.py）并更新 registry，
禁止直接修改已发布的版本文件——已生成的 ai_tasks.prompt_version
必须能精确追溯到当时的 prompt 文本。
"""
from typing import List

from .common import FACT_GROUNDING_RULES, facts_block, product_block

VERSION = "plan_v1"

# 4 种内容目标各自的生成逻辑（任务书 §5 铁律：不同目标用不同逻辑，
# 不要无条件套文艺风格）
GOAL_LOGIC = {
    "brand": (
        "品牌宣传：注重品牌识别、产品故事和视觉一致性。"
        "创意应围绕'这个品牌是谁、代表什么'展开，画面风格统一、有记忆点，"
        "避免硬推销，结尾落到品牌印象。"
    ),
    "sales": (
        "商品销售：注重真实购买理由、明确产品信息和适当行动引导。"
        "创意必须回答'为什么现在买、买什么规格、怎么买'，行动引导只用"
        "用户提供的真实购买信息，绝不编造价格、优惠、库存。"
    ),
    "intro": (
        "产品介绍：注重已确认的产品特征。"
        "创意围绕事实列表中的产品特征展开（产地/品种/口感/工艺等），"
        "没有事实依据的特征不写进创意。"
    ),
    "record": (
        "产地记录：注重真实场景和可核实的过程。"
        "创意必须是用户实拍条件能做到的真实记录（果园/作坊/日常），"
        "不虚构无法拍摄的场景，不美化无法核实的过程。"
    ),
}

GOAL_LABELS = {
    "brand": "品牌宣传",
    "sales": "商品销售",
    "intro": "产品介绍",
    "record": "产地记录",
}


def build_plan_messages(p, facts=None):
    goal = p.get("content_goal", "brand")
    goal_logic = GOAL_LOGIC.get(goal, GOAL_LOGIC["brand"])
    goal_label = GOAL_LABELS.get(goal, "品牌宣传")
    s = p.get("shoot", {}) or {}
    selling = p.get("selling_points") or []
    platforms = p.get("target_platforms") or []
    note_line = ""
    if s.get("note"):
        note_line = "- 特别注意：" + s["note"]
    prompt = (
        "你是一位短视频内容策划师，擅长为山东农产品商家做营销内容策划。\n"
        "本次内容目标是【" + goal_label + "】。\n\n"
        + goal_logic + "\n\n"
        + product_block(p.get("product", {})) + "\n\n"
        "已确认的产品事实（只能用这些做确定性描述）：\n"
        + facts_block(facts) + "\n"
        + FACT_GROUNDING_RULES + "\n\n"
        "策划输入：\n"
        "- 目标受众：" + str(p.get("audience", "（未指定）")) + "\n"
        "- 内容目的：" + str(p.get("content_purpose", "（未指定）")) + "\n"
        "- 核心信息：" + str(p.get("core_message", "（未指定）")) + "\n"
        "- 已确认卖点：" + ("、".join(selling) if selling else "（未指定）") + "\n"
        "- 创作风格：" + str(p.get("style", "（未指定，按目标自然选择，不要无条件套文艺风格）")) + "\n"
        "- 期望时长：" + str(p.get("target_duration_seconds", 30)) + " 秒；"
        "目标平台：" + ("、".join(platforms) if platforms else "（未指定）") + "\n"
        "- 实拍条件：设备 " + str(s.get("device", "（未指定）")) + "；"
        "人数 " + str(s.get("people", "（未指定）")) + "；"
        "灯光 " + str(s.get("lighting", "（未指定）")) + "；"
        "场景 " + str(s.get("location", "（未指定）")) + "\n"
        + note_line + "\n"
        "- 宣传限制：" + str(p.get("ad_restrictions", "（无）")) + "\n\n"
        "要求：\n"
        "1. 给出 3 到 5 个明确不同的候选创意方向（不要少于 3 个，不要多于 5 个）。\n"
        "2. 每个方向必须能在上述实拍条件下真实执行；执行不了的方向不要给，"
        "也不要宣称'可直接拍摄'。\n"
        "3. 严格遵守事实依据规则和宣传限制；风格按用户指定的来。\n\n"
        "只输出 JSON（不要任何解释文字）：\n"
        '{"candidates": [{"name": "方向名称", "goal": "' + goal + '", '
        '"core_expression": "核心表达（一句话）", '
        '"hook_opening": "开头切入点（前3秒怎么抓人）", '
        '"structure": "内容结构（开头-中间-结尾）", '
        '"suggested_shots": "建议画面（结合实拍条件）", '
        '"difficulty": "执行难度（低/中/高）", '
        '"shoot_notes": "执行注意事项", '
        '"fact_refs": ["依据的事实ID"]}]}'
    )
    return [{"role": "user", "content": prompt}]
