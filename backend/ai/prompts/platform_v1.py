"""V3.0 E: 多平台内容适配 prompt (platform_v1).

把一份已确认文案改写为目标平台的发布版本。

铁律 (同时是测试断言点):
- 只调整表达与结构, 绝不添加原文/已确认事实列表之外的:
  功效、产地认证、销量、排名、规格、质量等级、用户评价、价格、优惠、服务承诺.
- 平台规则 (字数/话题数等) 只是"建议参考", 不得写成平台官方硬性规定;
  更不得编造流量公式、爆款规律或任何效果承诺.
- 输出 JSON 顶层字段: title / body / hashtags / cover_text /
  pinned_comment / cta / changes / fact_notes.
"""
from __future__ import annotations

from typing import List

from .common import FACT_GROUNDING_RULES, facts_block
from .copyopt_v1 import FACT_RULES

VERSION = "platform_v1"

PLATFORM_DISCLAIMER = (
    "平台规则可能变化, 以下数值均为建议参考, 不是平台官方硬性规定; "
    "不要编造任何流量公式、爆款规律或效果承诺."
)


def build_platform_messages(source_text, source_structured, platform,
                            rules, facts=None, product_name="",
                            no_expand_claims=""):
    """拼装平台改写 prompt. platform/rules 非法由调用方先校验."""
    r = rules or {}
    title_r = r.get("title", {}) or {}
    body_r = r.get("body", {}) or {}
    tags_r = r.get("hashtags", {}) or {}
    cover_r = r.get("cover_text", {}) or {}
    parts = [
        "你是农产品短视频多平台发布专家. 只输出严格的 JSON (不要任何解释文字, 不要 markdown 代码块标记).",
        "任务: 把下面的已确认文案改写为【%s】平台的发布版本." % r.get("name", platform),
        "改写要求: 只调整表达、结构与篇幅以适应平台调性; 事实内容一个字都不能添、不能改、不能升级表述.",
        "平台调性参考: %s" % r.get("tone_guide", ""),
        "平台规则参考 (建议值, 尽量遵守, 但不得为凑字数删减关键信息):",
        "- 标题建议 %s 字以内: %s" % (title_r.get("suggest_max_chars", ""), title_r.get("tip", "")),
        "- 正文建议 %s 字以内: %s" % (body_r.get("suggest_max_chars", ""), body_r.get("tip", "")),
        "- 话题建议不超过 %s 个: %s" % (tags_r.get("suggest_max_count", ""), tags_r.get("tip", "")),
        "- 封面文案: %s" % cover_r.get("tip", ""),
        "- 行动引导风格: %s" % r.get("cta_style", ""),
        PLATFORM_DISCLAIMER,
    ]
    if source_structured:
        parts.append(
            "源文案 (结构化):\n标题: " + str(source_structured.get("title", ""))
            + "\n正文: " + str(source_structured.get("body", ""))
            + "\n话题: " + " ".join(str(t) for t in source_structured.get("hashtags", []))
            + "\n行动引导: " + str(source_structured.get("cta", "")))
    if source_text:
        parts.append("源文案 (正文):\n" + source_text)
    if product_name:
        parts.append("产品: " + product_name)
    if no_expand_claims:
        parts.append("产品宣传限制（禁止擅自扩展）: " + no_expand_claims)
    parts.append("已确认事实（只可用列表中的内容作为确定性描述）:\n" + facts_block(facts))
    parts.append(FACT_GROUNDING_RULES)
    parts.append(FACT_RULES)
    parts.append(
        "输出 JSON 顶层字段:\n"
        "title: 发布标题; body: 发布正文; hashtags: 话题标签数组 (不带#也可, 后端统一处理); "
        "cover_text: 封面文案; pinned_comment: 置顶评论建议; cta: 行动引导; "
        "changes: 针对该平台的调整说明数组; fact_notes: 涉及的产品事实说明")
    return [{"role": "user", "content": "\n\n".join(parts)}]


def validate_platform_result(d):
    """平台改写结果校验器. 返回 (errors, normalized)."""
    errs = []
    if not isinstance(d, dict):
        return ["结果不是 JSON 对象"], {}
    title = str(d.get("title", "") or "").strip()
    body = str(d.get("body", "") or "").strip()
    if not title:
        errs.append("缺少 title")
    if not body:
        errs.append("缺少 body")
    hashtags = d.get("hashtags", [])
    if not isinstance(hashtags, list):
        errs.append("hashtags 不是数组")
        hashtags = []
    hashtags = [str(t).strip().lstrip("#") for t in hashtags if str(t).strip()]
    out = {
        "title": title,
        "body": body,
        "hashtags": hashtags,
        "cover_text": str(d.get("cover_text", "") or "").strip(),
        "pinned_comment": str(d.get("pinned_comment", "") or "").strip(),
        "cta": str(d.get("cta", "") or "").strip(),
        "changes": [str(c) for c in (d.get("changes", []) or []) if str(c).strip()],
        "fact_notes": str(d.get("fact_notes", "") or ""),
    }
    return errs, out
