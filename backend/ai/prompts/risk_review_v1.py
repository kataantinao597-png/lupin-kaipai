"""risk_check prompt v1（原 backend/risk.py REVIEW_PROMPT_TMPL）。版本规则见 ideas_v1.py。"""
from typing import List

VERSION = "risk_review_v1"

REVIEW_PROMPT_TMPL = """你是短视频广告文案风险复核员。请审查以下旁白/字幕文案，找出**缺乏事实依据**的产品宣传。

用户输入的产品信息（视为用户自己的主张，文案与之相符的不标风险）：
{product_info}

用户已提供的证明材料：{evidence}

文案（逐条）：
{lines}

只关注**产品本身的事实宣传**：产地、品种、价格、销量、认证、农残、安全、功效。
不要标记拍摄手法类表述（如"实拍""不加滤镜""现场原声"），那是制作承诺不是产品宣传。

请特别注意以下**委婉功效暗示**（即使没有明显关键词也要标出）：
- 暗示食用后身体变好：如"身体更轻松""气色更好""每天吃一只，整个人都精神了"
- 暗示治疗/保健效果：如"坚持吃对血压好""老毛病都少了"

判定规则：
- 文案中的品种/规格/价格等事实如果与"用户输入的产品信息"一致 → 视为用户主张，**不标风险**
- 只标记文案中**超出用户输入**的、缺乏依据的说法，按以下分类：
  B. 用户提出但缺乏证明的宣传说法
  D. 需要用户补充证据的信息
- C类（无法核实）仅用于功效/医疗类暗示

只输出 JSON：{{"flags": [{{"text": "涉及的原文片段", "level": "高/中/低", "reason": "原因（含分类B/D）"}}]}}
没有风险则输出 {{"flags": []}}。不要输出其他文字。"""


def build_risk_review_messages(lines: List[str], evidence: str = "",
                               product_info: str = "") -> List[dict]:
    prompt = REVIEW_PROMPT_TMPL.format(
        product_info=product_info or "（未提供）",
        evidence=evidence or "（用户未提供证明材料）",
        lines="\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines)),
    )
    return [{"role": "user", "content": prompt}]
