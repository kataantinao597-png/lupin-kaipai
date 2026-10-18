"""storyboard prompt v1。版本规则见 ideas_v1.py。"""
from typing import List

from .common import FACT_GROUNDING_RULES, SHOT_FIELDS_DESC, facts_block, product_block

VERSION = "storyboard_v1"


def build_storyboard_messages(p: dict, direction_name: str, facts: list = None,
                              feedback: str = None) -> List[dict]:
    prompt = f"""你是短视频拍摄方案专家。请为以下产品生成可执行的拍摄分镜方案，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。

产品资料：
{product_block(p)}

已确认的产品事实：
{facts_block(facts)}

选定的创意方向：{direction_name}
{FACT_GROUNDING_RULES}

输出 JSON 顶层字段：
- product_summary: 产品资料摘要（字符串）
- target_audience: 目标受众（字符串）
- creative_direction: 创意方向名称（字符串）
- target_duration_seconds: 目标总时长（数字）
- shoot: {{"device": 拍摄设备, "people": 人数, "lighting": 灯光, "location": 场景}}
- shots: 镜头数组（{SHOT_FIELDS_DESC}）
- materials_checklist: 素材准备清单（字符串数组，从各镜头道具/素材汇总）
- risk_flags: 风险提示数组，每个元素 {{"text": 涉及的文案, "level": "高/中/低", "reason": 原因}}。凡旁白/字幕中出现缺乏依据的产地、销量、功效、认证宣传，必须在此标出
- pending_confirmations: 用户待确认事项（字符串数组）

约束：
1. 各镜头 duration_seconds 之和必须等于目标总时长（误差不超过2秒）
2. 拍摄动作必须在给定设备和人数下真实可行；单人手机拍摄时绝不能要求航拍、摇臂、多机位、轨道等专业设备
3. 旁白不要编造没有依据的销量、功效、认证数据；缺乏依据的说法只出现在 risk_flags 中，不进入旁白/字幕
4. 只输出 JSON，不要 markdown 代码块标记"""
    msgs = [{"role": "user", "content": prompt}]
    if feedback:
        msgs.append({"role": "user", "content": feedback})
    return msgs
