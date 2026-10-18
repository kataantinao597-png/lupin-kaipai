"""prompt 公共块（各版本 prompt 共用）。

注意：公共块的修改会影响所有引用它的 prompt 版本；
如需 breaking 变更，应新建版本文件而非直接改这里。
"""
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
