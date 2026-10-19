"""V3.0 D: 文案优化器 prompt (copyopt_v1).

5 种工作 (kind) 输入输出契约不同, 共用同一套事实禁令与风险纪律.
9 种模式 (mode) 只改变表达方向, 绝不改变事实含义.

铁律 (同时是测试断言点):
- 绝不添加原文/事实列表之外的: 功效、产地认证、销量、排名、规格、
  质量等级、用户评价、价格、优惠、服务承诺.
- 只优化表达, 不改变事实含义.
- 不要无条件套文艺风格: 默认 natural (实在口语), premium 等风格
  只有用户明确选择才用.
"""
from __future__ import annotations

from typing import List

from .common import FACT_GROUNDING_RULES, facts_block

VERSION = "copyopt_v1"

MODE_GUIDE = {
    "natural": "自然口语化: 像跟邻居拉呱一样说话, 去掉念稿腔和书面连接词, 实在、不端着",
    "premium": "品牌高级感: 克制、有质感的表达, 突出品牌调性, 忌浮夸、忌堆砌华丽辞藻",
    "shandong": "山东本土表达: 可用少量自然的地道表达, 绝不编造方言词汇表, 不堆砌生僻土语, 不低俗",
    "ecommerce": "电商转化表达: 给真实购买理由、把产品信息说清楚、适当行动引导; 不夸大、不承诺做不到的事",
    "hook": "开头吸引力优化: 重点改开头, 让人愿意继续看/听; 不做标题党, 开头必须与后文一致",
    "concise": "结构精简: 删冗余、留骨架, 信息一个不少, 逻辑更顺",
    "voiceover": "旁白口播优化: 适合直接念出来, 节奏顺口、断句自然, 忌拗口长句",
    "platform_post": "发布文案优化: 标题抓人但真实、正文清晰、话题相关、行动引导明确",
    "custom": "",
}

FACT_RULES = (
    "事实纪律 (违反即不合格):\n"
    "1. 绝不添加原文和已确认事实列表中没有的: 产品功效、产地认证、销量、排名、"
    "产品规格、质量等级、用户评价、价格、优惠、服务承诺.\n"
    "2. 只优化表达, 不改变事实含义; 原文的限定词不得改成肯定表述.\n"
    "3. 已确认事实列表为空时, 不得为凑卖点虚构任何事实.\n"
    "4. 用户自述内容仅为用户自述, 不代表外部验证; 不要替用户坐实未经证实的说法."
)

KIND_INTRO = {
    "optimize": "任务: 优化用户已有的文案 (见下文“原文”).",
    "generate": "任务: 从零生成新文案 (没有原文, 根据主题与要求创作).",
    "script": "任务: 生成完整视频脚本. 用【开场】/【主体】/【结尾】分段, 每段后可加一句画面提示.",
    "platform": "任务: 生成平台发布文案 (结构化输出: 标题/正文/话题/行动引导).",
    "adapt": "任务: 把同一份文案调整为适合目标平台的版本 (目标平台见“受众/目标平台”).",
}

KIND_OUTPUT = {
    "optimize": "text: 优化后的文案; changes: 改动说明数组; fact_notes: 涉及的产品事实说明",
    "generate": "text: 生成的文案; changes: 创作思路说明数组; fact_notes: 依据了哪些已确认事实",
    "script": "text: 完整视频脚本 (含【开场】/【主体】/【结尾】分段); changes: 结构说明数组; fact_notes: 涉及的产品事实说明",
    "platform": "title: 发布标题; body: 发布正文; hashtags: 话题标签数组; cta: 行动引导; changes: 创作说明数组; fact_notes: 涉及的产品事实说明",
    "adapt": "text: 调整后的文案; changes: 调整说明数组; fact_notes: 涉及的产品事实说明",
}


def build_copyopt_messages(
    kind: str,
    mode: str,
    source_text: str = "",
    goal: str = "",
    style: str = "",
    audience: str = "",
    length_limit: str = "",
    extra_requirements: str = "",
    custom_instruction: str = "",
    facts: list | None = None,
    product_name: str = "",
    no_expand_claims: str = "",
) -> List[dict]:
    """拼装文案优化 prompt. kind/mode 非法由调用方先校验."""
    mode_guide = MODE_GUIDE.get(mode, "")
    if mode == "custom":
        ci = custom_instruction or "（未填写，按自然口语化处理）"
        mode_guide = "用户自定义优化要求: " + ci
    parts = [
        "你是农产品短视频文案优化专家. 只输出严格的 JSON (不要任何解释文字, 不要 markdown 代码块标记).",
        KIND_INTRO[kind],
        "表达方向: " + mode_guide,
    ]
    if product_name:
        parts.append("产品: " + product_name)
    if source_text and kind in ("optimize", "adapt"):
        parts.append("原文:\n" + source_text)
    if kind == "adapt":
        parts.append("目标平台: " + (audience or "（未指定，按通用短视频平台处理）"))
    elif audience:
        parts.append("目标受众: " + audience)
    if goal:
        parts.append("优化目标: " + goal)
    if style:
        parts.append("风格约束: " + style)
    if length_limit:
        parts.append("长度要求: " + length_limit)
    if extra_requirements:
        parts.append("额外要求: " + extra_requirements)
    if no_expand_claims:
        parts.append("产品宣传限制（禁止擅自扩展）: " + no_expand_claims)
    parts.append("已确认事实（只可用列表中的内容作为确定性描述）:\n" + facts_block(facts))
    parts.append(FACT_GROUNDING_RULES)
    parts.append(FACT_RULES)
    parts.append("输出 JSON 顶层字段:\n" + KIND_OUTPUT[kind])
    return [{"role": "user", "content": "\n\n".join(parts)}]


def build_rewrite_paragraph_messages(
    full_text: str,
    paragraph: str,
    instruction: str,
    facts: list | None = None,
) -> List[dict]:
    """选中段落单独重写: 只改该段, 其他段落原样保留."""
    parts = [
        "你是农产品短视频文案优化专家. 只输出严格的 JSON (不要任何解释文字, 不要 markdown 代码块标记).",
        "任务: 只重写下面指定的一个段落, 全文其他段落必须原样保留 (一个字都不能改).",
        "全文:\n" + full_text,
        "要重写的段落:\n" + paragraph,
        "重写要求: " + (instruction or "自然口语化, 保持与其他段落衔接顺畅"),
        "已确认事实:\n" + facts_block(facts),
        FACT_GROUNDING_RULES,
        FACT_RULES,
        "输出 JSON 顶层字段: text (重写后的完整全文, 只有所选段落变化); changes (改动说明数组); fact_notes (涉及的产品事实说明)",
    ]
    return [{"role": "user", "content": "\n\n".join(parts)}]
