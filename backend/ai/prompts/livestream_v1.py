"""livestream prompt v1：农产品店播场控脚本。版本规则见 ideas_v1.py。"""
from typing import List

from.common import FACT_GROUNDING_RULES, facts_block, product_block

VERSION = "livestream_v1"

# 店播品类 → 节奏微调提示
CATEGORY_HINTS = {
"水果": "讲品时突出试吃环节（看得见的果肉/果汁），每轮循环安排一次切果试吃",
"蔬菜": "突出新鲜度和产地直发，讲储存方法和做法搭配",
"禽蛋肉": "突出检疫/溯源和做法，讲品时展示实物色泽",
"粮油": "突出产地和工艺，讲品节奏可放慢、讲透",
"加工食品": "突出配料表干净和试吃反馈，多读评论区",
"其他": "按通用节奏执行",
}


def build_livestream_messages(p: dict, facts: list = None, category: str = "水果",
                              duration_minutes: int = 120) -> List[dict]:
    hint = CATEGORY_HINTS.get(category, CATEGORY_HINTS["其他"])
    prompt = f"""你是农产品直播带货的场控专家。请为以下产品生成一份可直接照着念的店播场控脚本，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。

产品资料：
{product_block(p)}

已确认的产品事实：
{facts_block(facts)}

直播品类：{category}
直播总时长：{duration_minutes} 分钟
品类节奏提示：{hint}
{FACT_GROUNDING_RULES}

输出 JSON 顶层字段：
- title: 直播主题（一句话，口语化）
- total_minutes: 总时长（数字，等于 {duration_minutes}）
- segments: 时间轴分段数组，每个元素：
- phase: 阶段名，只能是以下之一：开场留人、讲品、互动、逼单、返场、收尾
- start_minute / end_minute: 起止分钟（数字）
- goal: 本段目标（一句话）
- script: 主播话术（字符串数组，每条是一句可直接念的口播，口语化、带山东农人的实在感）
- tips: 场控提示（字符串数组，如"此时上福袋""镜头给特写"）
- lucky_bag_points: 福袋互动点数组，每个元素 {{"at_minute": 分钟数, "name": 福袋名, "script": 吆喝话术}}
- closing_scripts: 转款逼单话术（字符串数组，3-5 条，针对犹豫不下单的观众）
- rescue_scripts: 冷场救场话术数组，每个元素 {{"scene": 冷场场景, "script": 救场话术}}，至少覆盖：没人说话、人数掉得快、只看不买 三种场景
- prep_checklist: 开播前准备清单（字符串数组）

约束：
1. 必须贴合农产品店播真实节奏：开场留人占前 5 分钟 → 之后每 20 分钟一个"讲品→互动→逼单"小循环 → 整点/半点安排福袋 → 最后 15 分钟返场+收尾逼单
2. 各 segment 的时间必须连续覆盖 0 到 {duration_minutes} 分钟，不重叠不留空
3. 话术必须是主播能直接念的口语，不要书面语；不要编造没有依据的销量、功效、认证数据
4. 价格、库存等商家未给的信息，用占位，绝不编造具体数字
5. 只输出 JSON，不要 markdown 代码块标记"""
    return [{"role": "user", "content": prompt}]
