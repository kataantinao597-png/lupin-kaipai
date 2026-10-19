"""storyboard prompt v2：AI 拍摄执行教练。

版本规则见 ideas_v1.py：禁止直接修改已发布的版本文件。

v2 相对 v1 的变化：
- 分镜从自由文本改为结构化可执行数据：每个镜头含拍摄步骤、手机摆放、
  验收标准、失败处理、补拍指引
- 时长拆分为录制时长 / 可用素材时长 / 成片使用时长，不再混淆
- 增加 video_purpose（brand/sales/intro/record），不同用途不同视频结构
- 只使用 shoot.available_resources 中列出的资源，不假设未确认的果园/果树/支架
"""
from typing import List, Optional

from .common import FACT_GROUNDING_RULES, SHOT_FIELDS_DESC, facts_block, product_block

VERSION = "storyboard_v2"

# 视频用途 → 结构要求
PURPOSE_STRUCTURE = {
    "brand": "品牌宣传：重视品牌识别、产品故事与视觉一致性；开头 3 秒必须出现品牌/产品名，结尾有品牌收束",
    "sales": "商品销售：必须有明确真实的购买理由、商品关键信息（规格/价格/购买方式，如用户未提供则留待确认项）、结尾有行动引导",
    "intro": "产品介绍：突出可核实的产品特征和使用场景；每个卖点必须有事实依据或标注待核实",
    "record": "产地记录：注重真实拍摄过程和场景证据；优先实拍流程、少摆拍，旁白如实描述所见",
}

# 运镜抽象词：出现时必须配具体做法，否则视为无效指令
ABSTRACT_MOVEMENT_WORDS = ["缓慢推近", "缓慢拉远", "缓慢移动", "轻轻晃动", "适当推进"]


def build_storyboard_v2_messages(
    p: dict,
    direction_name: str,
    video_purpose: str,
    available_resources: list,
    facts: list = None,
    feedback: str = None,
) -> List[dict]:
    """构建 v2 分镜生成的消息。

    p: 产品资料 dict（含 shoot.device/people/lighting/location/target_duration_seconds）
    direction_name: 选定的创意方向名称
    video_purpose: brand/sales/intro/record 四选一
    available_resources: 用户已确认可用的资源列表（如 ["新鲜樱桃果实", "手机支架", "阳台"]），
        AI 只能使用这些资源设计镜头，不得假设果园/果树/专业设备等未列出的资源
    facts: 已确认的产品事实
    feedback: 用户对上一版的修改意见（可选）
    """
    purpose_desc = PURPOSE_STRUCTURE.get(video_purpose, PURPOSE_STRUCTURE["intro"])
    resources_text = "\n".join(f"- {r}" for r in (available_resources or [])) or "（用户未列出，见下方约束）"

    prompt = f"""你是农产品短视频拍摄执行教练。你的目标不是写一份漂亮的文档，
而是让一个没有专业拍摄经验的用户，能拿着手机按步骤完成拍摄，并知道每段素材是否合格。

产品资料：
{product_block(p)}

已确认的产品事实：
{facts_block(facts)}
{FACT_GROUNDING_RULES}

选定的创意方向：{direction_name}

视频用途：{video_purpose} —— {purpose_desc}

用户已确认可用的资源（只能使用这些设计镜头）：
{resources_text}

资源使用铁律：
- 只能使用上面列出的资源设计镜头和动作
- 不得假设用户拥有果园、挂果的果树、采摘场景、手机支架、补光灯等未在列表中的资源
- 如某个好镜头需要未列出的资源，在 prerequisites 中写明"需要：XXX（用户未确认）"，
  并在 alt_approach 中给出只用已有资源的替代拍法

输出严格的 JSON（不要输出任何解释文字，只输出 JSON，不要 markdown 代码块标记）。
顶层字段：
- product_summary: 产品资料摘要（字符串）
- target_audience: 目标受众（字符串）
- creative_direction: 创意方向名称（字符串）
- video_purpose: 视频用途（brand/sales/intro/record 之一）
- target_duration_seconds: 目标总时长（数字）
- shoot: {{"device": 拍摄设备, "people": 人数, "lighting": 灯光, "location": 场景}}
- shots: 镜头数组，每个镜头包含以下全部字段：
{SHOT_FIELDS_DESC}
  v2 新增字段（每个镜头必填）：
  - record_duration_seconds: 建议现场录制时长（秒，数字，必须 ≥ final_duration_seconds，
    因为要多拍几条备选）
  - usable_duration_seconds: 预计可用素材时长（秒，数字）
  - final_duration_seconds: 成片使用时长（秒，数字；各镜头之和必须等于目标总时长，误差≤2秒）
  - shot_purpose: 镜头目的/叙事作用（一句话：这镜要完成什么表达任务）
  - prerequisites: 前置条件数组（拍这镜之前必须备好的；涉及未确认资源要明确标注）
  - shoot_steps: 按顺序的具体拍摄步骤（字符串数组，每一步都是可执行的动作，
    如"1. 把手机架在支架上，镜头与果盘平齐"；至少3步）
  - phone_setup: 手机摆放方式、主体在画面中的位置、建议拍摄距离、光线方向
    （如"手机用支架固定在桌面，樱桃放在画面中央偏下，距离约30cm，窗户光从左侧来"）
  - acceptance_criteria: 画面验收标准（字符串数组，每条必须是可观察的具体标准，
    如"樱桃占画面1/3以上、果皮高光清晰无过曝"；禁止"拍好看点""有食欲"这类主观词；至少2条）
  - failure_modes: 常见失败情况+解决步骤（数组，每项 {{"symptom": "可观察的失败现象", "fix": "具体解决步骤"}}；
    至少1个，如 {{"symptom": "画面抖动", "fix": "改用双手持机，肘部抵住桌面再录"}}）
  - reshoot_guide: 补拍指引（验收不通过时，具体怎么补拍，一句话可执行）
  - alt_approach: 替代拍法（原方案不可行时，只用已有资源的备选拍法）
  - transition: 转场说明（与上一个/下一个镜头的衔接方式）
  - edit_notes: 剪辑说明（这段素材怎么用、节奏、是否需要配音/音效）
- materials_checklist: 素材准备清单（字符串数组，从各镜头道具/素材汇总，只列可用资源中的）
- risk_flags: 风险提示数组，每个元素 {{"text": 涉及的文案, "level": "高/中/低", "reason": 原因}}。
  只标记当前脚本实际存在的风险（旁白/字幕/镜头说明中真实出现的说法），
  不要把通用提醒当成实际风险；缺乏依据的产地、销量、功效、认证宣传必须标出
- pending_confirmations: 用户待确认事项（字符串数组；风险提示里标出的事项不要重复列）

关键约束（违反则方案不合格）：
1. 每个运镜必须写清起幅/落幅/手持稳定方法/对焦方法。
   "缓慢推近""稍微拉远""适当移动"这类抽象词出现时，必须紧跟具体做法，
   如"缓慢推近：双手持机，从果盘全景开始，3秒内匀速靠近到樱桃占画面一半，焦点始终锁在果皮高光处"。
   没有具体做法的抽象运镜词视为无效指令。
2. 各镜头 final_duration_seconds 之和必须等于目标总时长（误差不超过2秒）；
   record_duration_seconds 必须 ≥ final_duration_seconds。
3. acceptance_criteria 必须是可观察的，禁止主观词。
4. 不同视频用途用不同结构（见上方用途说明），不要把所有用途套进同一种文艺模板。
5. 只输出 JSON，不要 markdown 代码块标记"""
    msgs = [{"role": "user", "content": prompt}]
    if feedback:
        msgs.append({"role": "user", "content": f"用户对上一版的修改意见（请据此调整，保持字段完整）：\n{feedback}"})
    return msgs
