"""V3.0 H: 效果复盘改进建议 prompt (review_v1).

根据已发布记录的人工录入指标 + 人工复盘笔记，给出改进建议。

铁律 (同时是测试断言点):
- 只给建议文本，不生成任何"已确认事实"之外的新事实主张
  （不编造播放量原因、不臆测平台算法、不承诺效果）。
- 建议只存建议表 (review_suggestions)，绝不自动修改已确认文案、
  已发布版本、发布记录的任何字段 —— 调用方保证只展示待人工采纳。
- 指标是"人工录入/平台截图导入"的，模型只能解读已有数据，
  不能推断缺失数据；直接观察与推测严格区分表述。
- 输出 JSON 顶层字段: suggestions (list of str) / observations
  (list of str, 只描述数据能直接看出的现象).
"""
from __future__ import annotations

VERSION = "review_v1"


def build_review_suggest_messages(record_title, platform, metrics_history,
                                  review_note):
    """拼装复盘建议 prompt.

    metrics_history: list of dict(source_label, date_range, metrics, note).
    review_note: 人工复盘笔记（可空）.
    """
    lines = [
        "你是农产品短视频内容复盘顾问. 只输出严格的 JSON (不要任何解释文字, 不要 markdown 代码块标记).",
        "任务: 针对下面这条已发布的视频，给出可执行的改进建议.",
        "发布记录标题: %s" % (record_title or "未命名"),
        "发布平台: %s" % (platform or "未指定"),
        "重要说明:",
        "1. 下面所有指标都是用户人工录入或从平台截图导入的真实数据, 你只能解读这些已有数据, 绝不能虚构、补全或推断缺失的指标.",
        "2. 直接观察 (observations) 只写数据能直接看出的现象；凡是推测都写进 suggestions 并明确标注'推测'.",
        "3. 建议要具体可执行 (改什么、怎么改), 不许写'提高内容质量'这类空话.",
        "4. 绝不编造新的产品事实主张；涉及产品卖点时只引用已确认事实, 不确定的写'需先核实'.",
        "5. 不承诺任何流量效果, 不编造平台算法规则.",
        "",
        "指标历史 (按录入顺序, 新的在后):",
    ]
    if metrics_history:
        for h in metrics_history:
            rng = h.get("date_range") or "未标注时间范围"
            metrics = h.get("metrics") or {}
            pairs = "、".join("%s=%s" % (k, v) for k, v in metrics.items())
            lines.append("- [%s | %s]%s%s" % (
                h.get("source_label", "人工录入"), rng,
                " " + pairs if pairs else "（无指标值）",
                "；备注: %s" % h["note"] if h.get("note") else ""))
    else:
        lines.append("（暂无录入指标）")
    lines += [
        "",
        "人工复盘笔记:",
        review_note.strip() or "（用户暂未写复盘笔记）",
        "",
        "输出 JSON: {\"observations\": [\"...\"], \"suggestions\": [\"...\"]}.",
        "observations 每条以'直接观察：'开头；suggestions 每条以'建议：'开头, 推测类加'（推测）'后缀.",
    ]
    return [
        {"role": "system",
         "content": "你是农产品短视频内容复盘顾问, 严格依据用户提供的真实数据给建议, 不虚构不承诺."},
        {"role": "user", "content": "\n".join(lines)},
    ]


def validate_review_suggest_result(result):
    """校验 AI 返回的建议结构. 返回标准化 dict, 失败抛 ValueError."""
    if not isinstance(result, dict):
        raise ValueError("result must be dict")
    obs = result.get("observations") or []
    sugs = result.get("suggestions") or []
    if not isinstance(obs, list) or not isinstance(sugs, list):
        raise ValueError("observations/suggestions must be list")
    obs = [str(x).strip() for x in obs if str(x).strip()][:10]
    sugs = [str(x).strip() for x in sugs if str(x).strip()][:10]
    if not sugs:
        raise ValueError("suggestions empty")
    return {"observations": obs, "suggestions": sugs}
