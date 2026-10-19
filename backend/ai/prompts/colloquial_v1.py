"""colloquial prompt v1：农人口语改写器。版本规则见 ideas_v1.py。

把书面口播文案改成山东农人的口语味：实在、唠嗑感，去"念稿腔"。
不编造方言词汇表——用自然口语加少量地道表达，不堆砌生僻土语。
"""
from typing import List

VERSION = "colloquial_v1"


def build_colloquial_messages(text: str, product_name: str = "") -> List[dict]:
    prompt = f"""你是山东农村短视频口播文案改写专家。请把下面这段书面口播文案，改写成山东农人自己说话的口语味，输出严格的 JSON（不要输出任何解释文字，只输出 JSON）。

产品：{product_name or "（未指定）"}
原文：
{text}

输出 JSON 顶层字段：
- rewritten: 改写后的口播文案（字符串，一段话，可直接念）
- changes: 改动说明（字符串数组，每条一句话，说明改了哪里、为什么这样改）

改写要求：
1. 口语化、唠嗑感：像跟邻居拉呱一样说话，去掉"念稿腔"和书面连接词
2. 山东味但克制：可用少量自然的地道表达（如"恁""怪好""得劲""实在"），绝不编造方言词汇表，不堆砌生僻土语，不低俗
3. 不改变原意：产品信息、卖点一个不能少，也一个不能多
4. 绝不添加原文没有的功效、销量、认证等宣传性说法
5. 长度与原文相当（上下浮动不超过 30%），适合口播节奏
6. 只输出 JSON，不要 markdown 代码块标记"""
    return [{"role": "user", "content": prompt}]
