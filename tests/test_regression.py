"""阶段 5：三案例回归。全流程 API 走通并记录：
ideas(3个) -> storyboard(校验/时长/单人约束) -> validate -> risk-check -> export。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8321"
EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"
FORBIDDEN = ["航拍", "摇臂", "多机位", "轨道", "无人机", "稳定器"]

results = []


def check(case, name, cond, detail=""):
    results.append((case, name, bool(cond), detail))
    print(f"[{'通过' if cond else '未通过'}][{case}] {name} {detail}", flush=True)


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def run_case(cid, case_file, risk_expect):
    t0 = time.time()
    case = json.load(open(f"{EXP}/{case_file}", encoding="utf-8"))
    product = {
        "product_name": case["product_name"],
        "description": case.get("description", ""),
        "selling_points": case["selling_points"],
        "price": case.get("price", ""),
        "audience": case.get("audience", ""),
        "evidence_materials": "",
        "shoot": case["shoot"],
    }
    # 1. ideas
    s, d = post("/api/ideas", {"product": product})
    check(cid, "ideas=3个", s == 200 and len(d.get("ideas", [])) == 3, f"HTTP{s}")
    if s != 200:
        return
    direction = d["ideas"][0]["name"] + "：" + d["ideas"][0]["tagline"]
    # 2. storyboard
    s, d = post("/api/storyboard", {"product": product, "direction": direction})
    ok = s == 200
    check(cid, "storyboard生成", ok, f"HTTP{s}")
    if not ok:
        return
    sb = d["storyboard"]
    total = sum(x["duration_seconds"] for x in sb["shots"])
    check(cid, "总时长误差≤2s", abs(total - sb["target_duration_seconds"]) <= 2,
          f"{total}s/{sb['target_duration_seconds']}s")
    bad = [x["shot_number"] for x in sb["shots"]
           if any(k in x["shooting_action"] + x["camera_movement"] for k in FORBIDDEN)]
    solo = case["shoot"]["device"] == "手机" and case["shoot"]["people"] == 1
    check(cid, "单人手机约束", not (solo and bad), f"违规镜头{bad}" if bad else "无违规")
    nums = [x["shot_number"] for x in sb["shots"]]
    check(cid, "序号连续", sorted(nums) == list(range(1, len(nums) + 1)))
    # 3. validate
    s, d = post("/api/validate", {"storyboard": sb})
    check(cid, "validate通过", s == 200 and d.get("valid"), f"HTTP{s}")
    # 4. risk-check
    s, d = post("/api/risk-check", {"storyboard": sb, "evidence_materials": "",
                                    "selling_points": case["selling_points"], "product": product})
    flags = d.get("flags", []) if s == 200 else []
    ftext = " ".join(f["text"] for f in flags)
    if risk_expect:
        hit = any(k in ftext for k in risk_expect)
        check(cid, "风险数据被标出", hit, f"{len(flags)}条")
    else:
        check(cid, "risk-check可用", s == 200, f"{len(flags)}条")
    # 5. export
    req = urllib.request.Request(BASE + "/api/export",
                                 data=json.dumps({"storyboard": sb}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
        ok = r.status == 200 and body[:2] == b"PK" and len(body) > 10000
        check(cid, "export下载docx", ok, f"{len(body)}B")
    except Exception as e:
        check(cid, "export下载docx", False, str(e)[:60])
    print(f"[{cid}] 用时 {time.time()-t0:.0f}s", flush=True)


run_case("A烟台苹果", "case_a.json", None)
run_case("B章丘大葱", "case_b.json", None)
run_case("C德州扒鸡", "case_c.json", ["全国销量第一", "降血压"])

print("\n==== 回归汇总 ====")
failed = [(c, n) for c, n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results)-len(failed)} 项")
for c, n in failed:
    print(f"  未通过: [{c}] {n}")
json.dump([{"case": c, "name": n, "pass": ok, "detail": d} for c, n, ok, d in results],
          open("/home/hatch/workspace/lupin-kaipai/demo/regression.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
sys.exit(1 if failed else 0)
