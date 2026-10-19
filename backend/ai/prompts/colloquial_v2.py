"""colloquial prompt v2：农人口语改写器（V3.1 事实 grounding 升级）。

v1 → v2 变更：
- 新增 facts 参数：服务端核对过的已确认事实（confirmed + allow_use + 未过期），
  由后端 FactRepository.list_usable() 提供，绝不信任前端传入的确认标志。
- 新增事实保真约束：改写只能使用原文 + 已确认事实中的信息；
  禁止新增未经确认的产品卖点、功效、销量、认证、价格、优惠承诺。
- 保留 v1 全部原则：口语化/唠嗑感、山东味克制、不改变原意、长度相当、只输出 JSON。

版本规则见 ideas_v1.py：v1 文件保留，registry 切到 v2。
"""
from typing import List

from .common import FACT_GROUNDING_RULES, facts_block

VERSION = "colloquial_v2"


def build_colloquial_messages(text: str, product_name: str = "",
                              facts: list = None) -> List[dict]:
    prompt = f"""你是山东农村短视频口播文案改写专家。请把下面这段书面口播文案，改写成山东农人自己说话的口语味，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。

产品：{product_name or "（未指定）"}

已确认的产品事实（可信来源，改写时涉及产品信息以此为准）：
{facts_block(facts)}

原文：
{text}
{FACT_GROUNDING_RULES}

输出 JSON 顶层字段：
- rewritten: 改写后的口播文案（字符串，一段话，可直接念）
- changes: 改动说明（字符串数组，每条一句话，说明改了哪里、为什么这样改）

改写要求：
1. 口语化、唠嗑感：像跟邻居拉呱一样说话，去掉"念稿腔"和书面连接词
2. 山东味但克制：可用少量自然的地道表达（如"恁""怪好""得劲""实在"），绝不编造方言词汇表，不堆砌生僻土语，不低俗
3. 不改变原意：原文的产品信息、卖点一个不能少，也一个不能多；已确认事实中没有的信息，不得写入改写结果
4. 事实保真：绝不添加原文和已确认事实中没有的功效、销量、认证、价格、优惠等宣传性说法；不确定的信息宁可不写，也不编造
5. 长度与原文相当（上下浮动不超过 30%），适合口播节奏
6. 只输出 JSON，不要 markdown 代码块标记"""
    return [{"role": "user", "content": prompt}]
