"""阶段 0.1：重试路径真实演练。

Test A: 单元 - 故意破坏的 JSON 必须被 validate_dict 报出具体错误（无模型）。
Test B1: 真实模型 + 恒失败校验器 -> 验证循环恰好尝试 4 次(1+3重试)、错误回传、最终抛用户可读错误。
Test B2: 真实模型 + 模拟首次输出缺字段 -> 验证错误反馈能让第 2 次调用产出合法结果。
"""
import json
import os
import sys
import copy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
BACKEND_DIR = os.path.join(ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


if "pytest" in sys.modules:  # O-12：pytest 采集时标记真调，默认集不带入
    import pytest
    pytestmark = pytest.mark.needs_model


def _skip(msg):
    print(f"[跳过] {msg}")
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(msg, allow_module_level=True)
    sys.exit(0)


def _has_api_key():
    try:
        from backend.ai_service import AIService
        return bool(AIService().api_key)
    except Exception:
        return False


from backend.validator import validate_dict
from backend.ai_service import AIService, AIFatalError

if __name__ == "__main__":
    EXP = os.path.join(
        os.path.dirname(ROOT), "goals",
        "shandong-ai-model-innovation-contest-registration",
        "experiments", "exp-2026-10-16",
    )
    if not os.path.isdir(EXP) and os.path.isdir(
            "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"):
        EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"
    if not os.path.isdir(EXP):
        _skip(f"缺少实验数据目录，无法运行重试演练：{EXP}")
    GOOD_SB = json.load(open(f"{EXP}/sb_caseA_attempt1.json", encoding="utf-8"))

    results = []


    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(f"[{'通过' if cond else '未通过'}] {name} {detail}")


    # ---------- Test A: 破坏性输入 ----------
    # A1: 缺必填字段
    bad1 = copy.deepcopy(GOOD_SB)
    del bad1["shots"][2]["subtitle"]
    errs, _ = validate_dict(bad1)
    check("A1 缺subtitle被报出", any("subtitle" in e for e in errs), f"errs={len(errs)}")

    # A2: 总时长不匹配
    bad2 = copy.deepcopy(GOOD_SB)
    bad2["target_duration_seconds"] = 60
    for s in bad2["shots"]:
        s["duration_seconds"] = 1  # 总和远小于60
    errs, _ = validate_dict(bad2)
    check("A2 总时长误差被报出", any("总时长" in e and "超过 2s" in e for e in errs))

    # A3: 序号不连续
    bad3 = copy.deepcopy(GOOD_SB)
    bad3["shots"][3]["shot_number"] = 99
    errs, _ = validate_dict(bad3)
    check("A3 序号不连续被报出", any("不连续" in e for e in errs))

    # A4: 单人手机违规词
    bad4 = copy.deepcopy(GOOD_SB)
    bad4["shoot"] = {"device": "手机", "people": 1}
    bad4["shots"][0]["shooting_action"] = "使用无人机航拍果园全景"
    errs, _ = validate_dict(bad4)
    check("A4 单人手机航拍被报出", any("约束" in e and "航拍" in e for e in errs))

    # A5: 正常数据通过
    errs, sb = validate_dict(GOOD_SB)
    check("A5 正常数据通过", errs == [] and sb is not None, f"errs={errs[:2]}")


    # ---------- Test B: 真实模型循环（需要真实 API Key，否则跳过） ----------
    if not _has_api_key():
        print("[跳过] Test B 需要真实 API Key（LUPIN_API_KEY），当前环境无密钥，仅运行 Test A")
    else:
        svc = AIService()  # 启动时现读 Key，放内存

        CASE_REQ = json.load(open(f"{EXP}/req_caseA_sb.json", encoding="utf-8"))
        BASE_MESSAGES = CASE_REQ["messages"]


        def make_messages(feedback):
            msgs = copy.deepcopy(BASE_MESSAGES)
            if feedback:
                msgs.append({"role": "user", "content": feedback})
            return msgs


        # B1: 恒失败校验器 -> 走满 4 次后抛用户可读错误
        calls_b1 = []
        def always_fail(data):
            return ["[测试] 人工恒失败（模拟不可修复的校验问题）"], None

        def counting_chat(msgs):
            calls_b1.append(msgs)
            return svc.chat(msgs, model="gpt-6-luna", temperature=0.3, json_object=True)

        try:
            svc.generate_with_retry(make_messages, always_fail, chat_fn=counting_chat)
            check("B1 4次失败后抛错", False, "未抛异常!")
        except AIFatalError as e:
            msg = str(e)
            check("B1 4次失败后抛用户可读错误", len(calls_b1) == 4 and "已重试 3 次" in msg,
                  f"attempts={len(calls_b1)}, msg头={msg[:50]}")
            # 验证错误反馈确实传回了模型（第2次请求的最后一条消息含反馈）
            fb2 = calls_b1[1][-1]["content"]
            check("B1 错误信息传回模型", "人工恒失败" in fb2)
        except Exception as e:
            check("B1 4次失败后抛用户可读错误", False, f"抛错类型不对: {type(e).__name__}: {e}")


        # B2: 首次输出被删字段(模拟模型失误) -> 第2次真实调用应修复成功
        corrupted = copy.deepcopy(GOOD_SB)
        del corrupted["shots"][1]["voiceover"]
        corrupted_json = json.dumps(corrupted, ensure_ascii=False)

        calls_b2 = []
        real_chat = lambda msgs: svc.chat(msgs, model="gpt-6-luna", temperature=0.3, json_object=True)
        def flaky_chat(msgs):
            calls_b2.append(msgs)
            if len(calls_b2) == 1:
                return corrupted_json, {"total_tokens": 0}  # 模拟首次模型输出缺字段
            return real_chat(msgs)

        try:
            obj, meta = svc.generate_with_retry(make_messages, validate_dict, chat_fn=flaky_chat)
            check("B2 缺字段经重试后成功", meta["attempts"] == 2 and obj is not None,
                  f"attempts={meta['attempts']}")
            fb = calls_b2[1][-1]["content"]
            check("B2 反馈含具体字段错误", "voiceover" in fb)
        except AIFatalError as e:
            check("B2 缺字段经重试后成功", False, f"意外失败: {str(e)[:80]}")

    print("\n==== 汇总 ====")
    failed = [n for n, ok, _ in results if not ok]
    print(f"共 {len(results)} 项，通过 {len(results)-len(failed)} 项")
if __name__ == "__main__":
    sys.exit(1 if failed else 0)
