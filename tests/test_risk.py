"""阶段 0.2：风险检查三层验证。
t1 明显极限词 -> 规则初检命中；t2 委婉暗示 -> 规则未命中(预期内)，模型复核应检出；
t3 正常介绍 -> 不得误报为高风险违规；t4 德州扒鸡实战分镜 -> 风险数据被隔离标记。"""
import json
import sys

sys.path.insert(0, "/home/hatch/workspace/lupin-kaipai")

from backend.risk import rule_scan, model_review, risk_check_storyboard
from backend.ai_service import AIService

EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'通过' if cond else '未通过'}] {name} {detail}")


# t1: 明显极限词
hits = rule_scan("本品全国销量第一，包治百病，降血压效果显著", "测试")
check("t1 规则命中极限词", len(hits) >= 3 and all(h["level"] == "高" for h in hits),
      f"命中{len(hits)}个: {[h['text'] for h in hits]}")

# t1b: 认证/农残类
hits = rule_scan("有机种植，零农残，绿色食品认证", "测试")
check("t1b 认证农残类命中", any(h["text"] == "有机" for h in hits) and any(h["text"] == "零农残" for h in hits))

svc = AIService()

# t2: 委婉功效暗示（规则层预期不命中，模型复核应检出）
hits_rule = rule_scan("每天吃一只，身体更轻松", "测试")
flags = model_review(svc, ["每天吃一只，身体更轻松"], evidence="")
texts = " ".join(f["text"] + f["reason"] for f in flags)
check("t2 规则层未命中(预期内)", len(hits_rule) == 0)
check("t2 模型复核检出功效暗示", len(flags) > 0 and ("轻松" in texts or "功效" in texts or "暗示" in texts),
      f"flags={len(flags)}: {texts[:80]}")

# t3: 正常介绍不误报高风险
flags3 = model_review(svc, ["果园现摘，48小时内发货"], evidence="")
high = [f for f in flags3 if f["level"] == "高"]
check("t3 正常介绍无高风险误报", len(high) == 0, f"复核flags={len(flags3)}")

# t4: 德州扒鸡实战
sb_c = json.load(open(f"{EXP}/sb_caseC_attempt1.json", encoding="utf-8"))
res = risk_check_storyboard(svc, sb_c, evidence="用户称百年老字号有依据但未提供证明材料",
                            input_claims=["百年老字号工艺", "全国销量第一", "吃了能降血压"])
flag_texts = " ".join(f["text"] for f in res["flags"])
check("t4 实战检出风险数据", "全国销量第一" in flag_texts or "降血压" in flag_texts,
      f"共{len(res['flags'])}条")
# 风险说法不应出现在旁白/字幕正文（被隔离）
body = " ".join(s.get("voiceover", "") + s.get("subtitle", "") for s in sb_c["shots"])
check("t4 风险说法未进入旁白字幕", "全国销量第一" not in body and "降血压" not in body)
# confirmed 默认 False（人工确认入口）
check("t4 flag带confirmed字段", all("confirmed" in f and f["confirmed"] is False for f in res["flags"]))

print("\n==== 汇总 ====")
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results)-len(failed)} 项")
sys.exit(1 if failed else 0)
