"""鲁品开拍 - 宣传事实风险检查（三层：规则初检 + 模型复核 + 人工确认）。

不声称广告合规保证；只做"依据缺失"提示，最终由人工确认。
"""
import json
from typing import Dict, List, Optional

from .ai_service import DEFAULT_MODEL, parse_json_content

# 第一层：关键词表（按类别组织；命中 -> 初检 flag）
RISK_WORDS = {
    # (关键词, 等级, 原因)
    "极限词": [
        ("最", "高", "极限词'最'需具体依据"), ("第一", "高", "极限词'第一'需权威数据支撑"),
        ("首家", "高", "'首家'需可验证的事实依据"), ("首选", "高", "'首选'属主观极限表述"),
        ("顶级", "高", "'顶级'需明确评判标准"), ("极致", "高", "极限表述"),
        ("之王", "高", "极限表述"), ("国家级", "高", "'国家级'需官方认定文件"),
        ("世界级", "高", "'世界级'需权威依据"), ("百分之百", "高", "绝对化表述"),
        ("100%", "高", "绝对化表述"), ("零风险", "高", "绝对化表述"),
        ("无任何副作用", "高", "绝对化安全宣称"),
    ],
    "销量宣称": [
        ("销量第一", "高", "销量排名需第三方权威数据"), ("全国销量第一", "高", "销量排名需第三方权威数据"),
        ("全网第一", "高", "销量排名需第三方权威数据"), ("月销过万", "中", "销量数据需销售记录支撑"),
        ("爆卖", "中", "销量表述需数据支撑"),
    ],
    "功效宣称": [
        ("降血压", "高", "功效宣称需科学依据/批文"), ("降血糖", "高", "功效宣称需科学依据/批文"),
        ("降血脂", "高", "功效宣称需科学依据/批文"), ("减肥", "高", "功效宣称需科学依据"),
        ("瘦身", "高", "功效宣称需科学依据"), ("排毒", "高", "功效宣称缺乏公认依据"),
        ("抗癌", "高", "医疗功效宣称风险极高"), ("治疗", "高", "医疗功效宣称风险极高"),
        ("治愈", "高", "医疗功效宣称风险极高"), ("根治", "高", "医疗功效宣称风险极高"),
        ("包治百病", "高", "虚假功效宣称"), ("增强免疫", "中", "功效暗示需依据"),
        ("养颜", "中", "功效暗示需依据"),
    ],
    "认证宣称": [
        ("有机", "中", "'有机'需有机认证证书"), ("绿色食品", "中", "需绿色食品认证"),
        ("无公害", "中", "需无公害认证"), ("地理标志", "中", "需地理标志证明"),
        ("驰名商标", "中", "需驰名商标认定"), ("老字号", "中", "'老字号'需官方认定或历史依据"),
        ("祖传", "中", "'祖传'需可说明的传承依据"), ("秘方", "中", "'秘方'表述需谨慎"),
    ],
    "农残安全": [
        ("零农残", "中", "'零农残'需检测报告"), ("无农药", "中", "'无农药'需检测报告"),
        ("无添加", "中", "'无添加'需配料表/检测支撑"), ("零添加", "中", "'零添加'需配料表/检测支撑"),
        ("纯天然", "中", "'纯天然'需依据且易引误解"),
    ],
}

# 委婉功效暗示模式（规则层抓不到，交给模型复核重点识别）
HINT_PATTERNS = ["更轻松", "更健康", "身体好", "气色好", "睡得香", "有精神"]


def rule_scan(text: str, where: str = "") -> List[Dict]:
    """第一层：关键词扫描。返回 flag 列表。"""
    flags = []
    for _cat, words in RISK_WORDS.items():
        for kw, level, reason in words:
            if kw in text:
                flags.append({
                    "text": kw, "level": level,
                    "reason": f"{reason}（出现位置：{where}）",
                    "source": "规则初检", "confirmed": False,
                })
    return flags


from .ai.prompts import REVIEW_PROMPT_TMPL, get_prompt_version  # 141-160：版本化 prompt
from .ai.prompts.risk_review_v1 import build_risk_review_messages


def model_review(ai_service, lines: List[str], evidence: str = "",
                   product_info: str = "", model: Optional[str] = None) -> List[Dict]:
    """第二层：模型复核。lines 为待审文案列表。

    model: 用户选择的模型；为空则使用服务默认模型。
    """
    prompt = REVIEW_PROMPT_TMPL.format(
        product_info=product_info or "（未提供）",
        evidence=evidence or "（用户未提供证明材料）",
        lines="\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines)),
    )
    # 121：走 AI 网关（task_type="risk_check"）；兼容直接传 AIService 的老调用
    from .ai.gateway import AIGateway
    if isinstance(ai_service, AIGateway):
        content, _usage = ai_service.chat(
            "risk_check",
            build_risk_review_messages(lines, evidence, product_info),
            model=model,  # None → 走 128 任务选模型策略；显式指定则优先
            temperature=0.2, json_object=True,
            prompt_version=get_prompt_version("risk_check"),
        )
    else:
        content, _usage = ai_service.chat(
            [{"role": "user", "content": prompt}],
            model=model or DEFAULT_MODEL,
            temperature=0.2, json_object=True,
            task_type="risk_check",
        )
    # 统一 JSON 解析：兼容模型输出 markdown 代码围栏/前后杂文本
    data = parse_json_content(content)
    raw_flags = data.get("flags", []) if isinstance(data, dict) else []
    flags = []
    for f in raw_flags:
        if not isinstance(f, dict):
            continue
        flags.append({
            "text": f.get("text", ""), "level": f.get("level", "中"),
            "reason": f.get("reason", ""), "source": "模型复核", "confirmed": False,
        })
    return flags


def dedup(flags: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for f in flags:
        key = (f["text"], f["level"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def risk_check_storyboard(ai_service, sb: dict,  # ai_service: AIGateway 或 AIService evidence: str = "",
                          input_claims: List[str] | None = None,
                          product_info: str = "",
                          model: Optional[str] = None) -> Dict:
    """对分镜的旁白/字幕 + 用户原始卖点做三层检查（前两层自动，第三层人工确认由前端完成）。

    input_claims: 用户输入的卖点/产品信息原文，用于发现"输入即带风险"的情况。
    model: 用户选择的模型，透传给模型复核；为空则使用服务默认模型。
    返回 {"flags": [...], "scanned_shots": n}。
    """
    lines = []
    for s in sb.get("shots", []):
        n = s.get("shot_number")
        lines.append(f"[镜头{n}旁白] {s.get('voiceover', '')}")
        lines.append(f"[镜头{n}字幕] {s.get('subtitle', '')}")
    flags: List[Dict] = []
    for ln in lines:
        where = ln.split("]")[0].strip("[")
        flags.extend(rule_scan(ln, where))
    for claim in input_claims or []:
        for f in rule_scan(claim, "用户输入卖点"):
            f["reason"] += "；注意：该说法来自用户输入，请确认是否有证明材料"
            flags.append(f)
    flags.extend(model_review(ai_service, lines, evidence, product_info, model=model))
    return {"flags": dedup(flags), "scanned_shots": len(sb.get("shots", []))}


# ---------- V5+: 事实 grounding 规则层 ----------

def check_fact_grounding(sb_dict: dict, facts: list) -> list:
    """规则层：检查旁白/字幕中的事实宣称是否有事实依据。
    
    有效引用要求：事实 ID 存在，且 status=confirmed，且 allow_use=true。
    返回 [{"shot_number": n, "text": 涉及文案, "level": "中", "reason": 原因}]
    不依赖模型，确定性检查。
    """
    facts = facts or []
    known_ids = {f.get("id") for f in facts if f.get("id")}
    # 只有"已确认且允许使用"的事实才能作为宣传依据
    usable_ids = {
        f.get("id") for f in facts
        if f.get("id") and f.get("status") == "confirmed" and f.get("allow_use") is True
    }
    flags = []
    for s in sb_dict.get("shots", []):
        num = s.get("shot_number")
        text = f"{s.get('voiceover','')} {s.get('subtitle','')}"
        refs = s.get("fact_refs") or []
        # 1. 引用的事实 ID 不存在
        missing_refs = [r for r in refs if r not in known_ids]
        if missing_refs:
            flags.append({
                "shot_number": num,
                "text": f"引用了不存在的事实ID: {','.join(missing_refs)}",
                "level": "中",
                "reason": "事实引用无效，请核实",
            })
        # 2. 引用的事实存在但未确认或不允许使用
        unusable_refs = [r for r in refs if r in known_ids and r not in usable_ids]
        if unusable_refs:
            flags.append({
                "shot_number": num,
                "text": f"引用的事实未确认或不允许使用: {','.join(unusable_refs)}",
                "level": "中",
                "reason": "宣传依据必须是 status=confirmed 且允许使用的事实，请先在事实库中确认",
            })
        # 2. 含高风险宣称但无事实引用
        has_risky_claim = any(
            kw in text
            for cat in RISK_WORDS.values()
            for kw, _, _ in cat
        )
        if has_risky_claim and not refs:
            flags.append({
                "shot_number": num,
                "text": text[:60],
                "level": "中",
                "reason": "含产品事实宣称但未关联已确认事实，请核实依据",
            })
    return flags
