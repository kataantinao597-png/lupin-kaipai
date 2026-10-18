"""feasibility prompt v1。版本规则见 ideas_v1.py。"""
from typing import List

VERSION = "feasibility_v1"


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
