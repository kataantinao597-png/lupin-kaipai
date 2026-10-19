"""livestream prompt v2：农产品店播场控脚本（V3.1 增量升级）。

v1 → v2 变更（向后兼容，核心约束不变）：
- 新增 extra 可选输入：直播平台 / 直播目标 / 主播风格 / 促销安排
  （前端 v5 可选填；为空时不影响原有节奏逻辑）。
- 输出新增 pending_confirms：待确认清单（字符串数组）——价格、库存、
  促销时间、认证等未确认信息必须用明显占位符，并在清单中逐项列出，
  不得编造稀缺感、销量、产品功效或认证。
- 其余约束（FACT_GROUNDING_RULES、CATEGORY_HINTS、六阶段、时间轴连续
  覆盖、占位符不编造）与 v1 一致。

版本规则见 ideas_v1.py：v1 文件保留，registry 切到 v2。
"""
from typing import List

from . import livestream_v1
from .common import FACT_GROUNDING_RULES, facts_block, product_block

VERSION = "livestream_v2"

# 复用 v1 的品类节奏微调与阶段枚举
CATEGORY_HINTS = livestream_v1.CATEGORY_HINTS


def _extra_block(extra: dict) -> str:
    """可选输入块：只渲染用户实际填写的项。"""
    if not extra:
        return "（未提供，按通用店播节奏执行）"
    labels = {"platform": "直播平台", "goal": "直播目标",
              "style": "主播风格", "promo": "促销安排"}
    lines = []
    for k, label in labels.items():
        v = (extra or {}).get(k)
        if v and str(v).strip():
            lines.append("- {}：{}".format(label, str(v).strip()))
    return "\n".join(lines) if lines else "（未提供，按通用店播节奏执行）"


def build_livestream_messages(p: dict, facts: list = None,
                              category: str = "水果",
                              duration_minutes: int = 120,
                              extra: dict = None) -> List[dict]:
    hint = CATEGORY_HINTS.get(category, CATEGORY_HINTS["其他"])
    prompt = (
        "你是农产品直播带货的场控专家。请为以下产品生成一份可直接照着念的"
        "店播场控脚本，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。\n\n"
        "产品资料：\n" + product_block(p) + "\n\n"
        "已确认的产品事实：\n" + facts_block(facts) + "\n\n"
        "直播品类：" + category + "\n"
        "直播总时长：" + str(duration_minutes) + " 分钟\n"
        "品类节奏提示：" + hint + "\n\n"
        "本场直播补充信息（可选，未提供的按通用节奏执行）：\n"
        + _extra_block(extra) + "\n"
        + FACT_GROUNDING_RULES + "\n\n"
        "输出 JSON 顶层字段：\n"
        "- title: 直播主题（一句话，口语化）\n"
        "- total_minutes: 总时长（数字，等于 " + str(duration_minutes) + "）\n"
        "- segments: 时间轴分段数组，每个元素：\n"
        "- phase: 阶段名，只能是以下之一：开场留人、讲品、互动、逼单、返场、收尾\n"
        "- start_minute / end_minute: 起止分钟（数字）\n"
        "- goal: 本段目标（一句话）\n"
        "- script: 主播话术（字符串数组，每条是一句可直接念的口播，口语化、带山东农人的实在感）\n"
        "- tips: 场控提示（字符串数组，如“此时上福袋”“镜头给特写”）\n"
        "- lucky_bag_points: 福袋互动点数组\n"
        "- closing_scripts: 转款逼单话术（字符串数组，3-5 条）\n"
        "- rescue_scripts: 冷场救场话术数组，至少覆盖：没人说话、人数掉得快、只看不买\n"
        "- prep_checklist: 开播前准备清单（字符串数组）\n"
        "- pending_confirms: 待确认清单（字符串数组）——价格、库存、促销时间、认证等"
        "未确认信息逐项列出；全部已确认时为空数组\n\n"
        "约束：\n"
        "1. 贴合农产品店播真实节奏：开场留人占前 5 分钟 → 之后每 20 分钟一个"
        "“讲品→互动→逼单”小循环 → 整点/半点安排福袋 → 最后 15 分钟返场+收尾逼单；"
        "长场（≥120 分钟）与短场（<60 分钟）循环密度不同，不得机械套用\n"
        "2. 各 segment 时间必须连续覆盖 0 到 " + str(duration_minutes)
        + " 分钟，不重叠不留空\n"
        "3. script 只放主播可直接念的话术；场控提示、镜头动作、现场执行任务一律放 tips\n"
        "4. 不要编造没有依据的销量、功效、认证数据\n"
        "5. 价格、库存、促销时间、认证等未确认信息，用【……待确认】明显占位，"
        "绝不编造具体数字；所有占位项同步列入 pending_confirms；不得编造稀缺感\n"
        "6. 只输出 JSON，不要 markdown 代码块标记"
    )
    return [{"role": "user", "content": prompt}]


def build_regen_segment_messages(p: dict, facts: list = None,
                                 category: str = "水果",
                                 duration_minutes: int = 120,
                                 segment: dict = None,
                                 extra: dict = None) -> List[dict]:
    """单段重生成：只重写指定时间段的话术，不动时间轴。"""
    seg = segment or {}
    prompt = (
        "你是农产品直播带货的场控专家。现在只需要重写店播脚本中的"
        "**某一个时间段**的话术，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。\n\n"
        "产品资料：\n" + product_block(p) + "\n\n"
        "已确认的产品事实：\n" + facts_block(facts) + "\n\n"
        "直播品类：" + category + "，本场总时长 " + str(duration_minutes) + " 分钟\n"
        "本场直播补充信息：\n" + _extra_block(extra) + "\n"
        + FACT_GROUNDING_RULES + "\n\n"
        "要重写的时间段（起止时间固定，不许改动）：\n"
        "- phase: " + str(seg.get("phase")) + "\n"
        "- start_minute: " + str(seg.get("start_minute"))
        + "，end_minute: " + str(seg.get("end_minute")) + "\n"
        "- goal: " + str(seg.get("goal") or "（沿用原目标）") + "\n\n"
        "输出 JSON 顶层字段（只输出这一个段）：\n"
        "- phase: 阶段名（必须与给定 phase 一致）\n"
        "- start_minute / end_minute: 起止分钟（必须与给定数值一致）\n"
        "- goal: 本段目标（一句话）\n"
        "- script: 主播话术（字符串数组，每条是一句可直接念的口播）\n"
        "- tips: 场控提示（字符串数组）\n\n"
        "约束：\n"
        "1. phase、start_minute、end_minute 必须与给定值完全一致，不得改动时间轴\n"
        "2. script 只放主播可直接念的话术，场控提示放 tips\n"
        "3. 价格、库存、促销时间、认证等未确认信息用【……待确认】占位，"
        "绝不编造具体数字；不得编造销量、功效、认证、稀缺感\n"
        "4. 只输出 JSON，不要 markdown 代码块标记"
    )
    return [{"role": "user", "content": prompt}]
