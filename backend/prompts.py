"""鲁品开拍 - 提示词构造（由实验 build_idea_req.py / build_sb_req.py 移植并结构化）。"""
from typing import List

SHOT_FIELDS_DESC = """每个镜头必须包含以下字段：
- shot_number: 镜头序号（整数，从1开始连续）
- duration_seconds: 镜头时长（秒，数字，必须>0）
- shot_type: 景别，只能是以下之一：特写、近景、中景、中远景、全景、远景
- scene_description: 画面内容描述
- shooting_action: 实际拍摄动作（必须在给定拍摄条件下可执行）
- voiceover: 旁白文案
- subtitle: 字幕文字
- props: 所需道具（字符串数组）
- required_assets: 所需素材（字符串数组）
- camera_movement: 运镜方式
- evidence_notes: 若旁白/字幕涉及产品事实（产地、功效、销量、认证等），写明依据来源或"待核实"；无则写"无"
- fact_refs: 本镜头依据的事实ID数组（如 ["f1"]），无依据则为空数组
"""


def facts_block(facts: list) -> str:
    """已确认事实块：只传入 allow_use 且 status=confirmed 的。"""
    usable = [f for f in (facts or []) if f.get("allow_use") and f.get("status") == "confirmed"]
    if not usable:
        return "（用户未提供已确认的产品事实）"
    lines = []
    for f in usable:
        lines.append(f"- [事实{f.get('id')}] {f.get('content')}（分类：{f.get('category','其他')}；来源：{f.get('source') or '用户提供'}）")
    return "\n".join(lines)


FACT_GROUNDING_RULES = """
事实依据规则（必须遵守）：
1. 只能将"已确认事实"列表中的内容作为确定性产品描述（产地/品种/口感/功效/销量/认证等）
2. 绝不编造未在事实列表中的产地、销量、功效、认证信息
3. 事实列表为空或不足时，生成不依赖缺失事实的内容，不得为凑卖点虚构
4. 每个创意/镜头的 fact_refs 字段填写所依据的事实ID（如 ["f1","f3"]）；无依据则留空数组
5. 用户填写的内容仅为用户自述，不代表外部验证；风险提示仍需人工核实
"""

def product_block(p: dict) -> str:
    s = p.get("shoot", {})
    lines = [
        f"- 产品：{p.get('product_name','')}",
        f"- 产品信息：{p.get('description','')}",
        f"- 卖点：{'、'.join(p.get('selling_points', []))}",
        f"- 价格：{p.get('price','')}",
        f"- 目标受众：{p.get('audience','')}",
        f"- 证明材料：{p.get('evidence_materials','（未提供）')}",
        f"- 拍摄设备：{s.get('device','')}；人数：{s.get('people','')}人；灯光：{s.get('lighting','')}；场景：{s.get('location','')}",
        f"- 目标视频总时长：{s.get('target_duration_seconds','')}秒",
    ]
    if s.get("note"):
        lines.append(f"- 特别注意：{s['note']}")
    return "\n".join(lines)


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


def build_storyboard_messages(p: dict, direction_name: str, facts: list = None, feedback: str = None) -> List[dict]:
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


def build_regen_shot_messages(sb: dict, shot_number: int, instruction: str,
                              locked: List[int], feedback: str = None) -> List[dict]:
    import json as _json
    prompt = f"""你是短视频分镜师。请根据以下完整分镜方案，只重新生成第 {shot_number} 号镜头。

完整分镜（JSON）：
{_json.dumps(sb, ensure_ascii=False)[:6000]}

修改指令：{instruction}

锁定镜头（绝对不许改动）：{locked if locked else "无"}

要求：
1. 只输出重新生成的单个镜头 JSON，字段与原分镜的 shot 字段完全一致（shot_number 保持为 {shot_number}）
2. 新镜头的拍摄动作必须与前后镜头在逻辑上衔接
3. 时长尽量与原镜头一致；如需调整，总时长变化不要超过2秒
4. 保持单人手机等拍摄约束；不要编造无依据的销量/功效/认证
5. 只输出 JSON，不要任何解释文字"""
    msgs = [{"role": "user", "content": prompt}]
    if feedback:
        msgs.append({"role": "user", "content": feedback})
    return msgs


def build_feasibility_messages(sb: dict, feedback: str = None) -> List[dict]:
    """批量检查整组镜头的拍摄可行性。一次请求覆盖所有镜头。"""
    import json as _json
    shoot = sb.get("shoot", {})
    shots_brief = [
        {"shot_number": s.get("shot_number"),
         "duration_seconds": s.get("duration_seconds"),
         "shot_type": s.get("shot_type"),
         "scene_description": s.get("scene_description"),
         "shooting_action": s.get("shooting_action"),
         "camera_movement": s.get("camera_movement"),
         "props": s.get("props", []),
         "required_assets": s.get("required_assets", [])}
        for s in sb.get("shots", [])
    ]
    prompt = f"""你是短视频拍摄可行性评估师。请判断以下每个镜头在用户实际拍摄条件下是否可执行。

用户拍摄条件：
- 设备：{shoot.get('device', '未知')}
- 人数：{shoot.get('people', '未知')}人
- 场景：{shoot.get('location', '未知')}
- 灯光：{shoot.get('lighting', '未知')}
- 目标总时长：{sb.get('target_duration_seconds', '未知')}秒

镜头列表（JSON）：
{_json.dumps(shots_brief, ensure_ascii=False)}

请逐镜头检查以下 6 个方面：
1. 设备条件：需要的设备是否与用户已有设备一致
2. 人员条件：是否需要额外摄影师、演员、助手或多人配合
3. 场地条件：是否依赖用户未提供或无法确认的特殊场地
4. 动作可执行性：镜头运动、角度、动作能否由当前人员设备完成
5. 道具条件：是否需要未提供、难以获得或不合理的道具
6. 时间与难度：是否在目标时长内明显无法完成

判断标准：
- "可执行"：在给定条件下可直接拍摄
- "建议调整"：有困难但可通过简化解决
- "不可执行"：明显超出条件（如单人手机要求无人机航拍）
- "需要人工确认"：信息不足无法判断，**不要编造**设备、场地、人员信息

对"建议调整"/"不可执行"的镜头，必须给出替代方案：
- 只用用户已有的设备和人员，不增加预算/场地/专业器材
- 保留原镜头想表达的产品特色和信息目的
- 替代方案的建议时长尽量与原镜头一致；如需改变，明确说明
- 不要编造产品事实、销量、功效承诺

对确实可行的镜头，**不要**为了凑结果强行生成替代方案（alternative 设为 null）。

只输出 JSON（不要任何解释文字）：
{{"results": [{{"shot_number": 镜头号,
  "status": "可执行/建议调整/不可执行/需要人工确认",
  "reason": "具体原因（一句话）",
  "constraints_involved": ["涉及的约束，如：设备条件"],
  "alternative": null 或 {{"shooting_method": "拍摄方式",
    "device_usage": "现有设备的具体用法",
    "camera_position": "机位/角度",
    "action": "人物及产品动作",
    "props_needed": ["道具/场地要求"],
    "suggested_duration": 建议时长数字,
    "preserved_purpose": "保留的原始表达目的"}}}}]}}
"""
    msgs = [{"role": "user", "content": prompt}]
    if feedback:
        msgs.append({"role": "user", "content": feedback})
    return msgs
