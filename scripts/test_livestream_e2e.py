"""V3.1 店播链路实测：建任务 → 轮询 → 记录结果；失败则把错误原文打出来。"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"


def req(method, path, body=None, token=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def jd(raw):
    d = json.loads(raw)
    return d.get("data", d) if isinstance(d, dict) else d


s, b = req("POST", "/api/v1/auth/login",
           {"email": "demo@lupin.test", "password": "demo1234"})
tok = json.loads(b)["access_token"]
oid = jd(req("GET", "/api/v1/orgs", token=tok)[1])[0]["id"]

body = {
    "task_type": "livestream_script",
    "org_id": oid,
    "payload": {
        "product": {"product_name": "烟台苹果", "category": "水果",
                    "origin": "烟台栖霞", "selling_points": ["脆甜多汁", "现摘现发"]},
        "facts": [],
        "category": "水果",
        "duration_minutes": 60,
        "extra": {"platform": "抖音", "goal": "清库存", "style": "实在唠嗑",
                  "promo": "整点福袋"},
    },
}
s, b = req("POST", "/api/v1/ai-tasks", body, token=tok,
           headers={"Idempotency-Key": "livestream-e2e-001"})
print("建任务", s, b[:200])
if s not in (200, 202):
    sys.exit(1)
tid = jd(b)["id"]

t0 = time.time()
last = None
while time.time() - t0 < 420:
    time.sleep(5)
    s, b = req("GET", f"/api/v1/ai-tasks/{tid}", token=tok)
    d = jd(b)
    st = d.get("status")
    if st != last:
        print(f"  [{int(time.time() - t0):3d}s] {st} progress={d.get('progress')}")
        last = st
    if st in ("succeeded", "failed", "cancelled"):
        break

d = jd(req("GET", f"/api/v1/ai-tasks/{tid}", token=tok)[1])
print("终态:", d.get("status"), "耗时", int(time.time() - t0), "s")
print("model_used:", d.get("model_used"), "prompt:", d.get("prompt_version"))
if d.get("status") == "succeeded":
    r = d.get("result") or {}
    sc = r.get("livestream_script") or {}
    segs = sc.get("segments") or []
    print(f"标题: {sc.get('title')} | total={sc.get('total_minutes')} | 段数={len(segs)}")
    for x in segs:
        print(f"  {x.get('phase')} {x.get('start_minute')}-{x.get('end_minute')} 句数={len(x.get('script') or [])}")
    print("待确认:", len(sc.get("pending_confirms") or []), "| 福袋:", len(sc.get("lucky_bag_points") or []))
    print("prompt_version:", sc.get("prompt_version"), "| schema:", sc.get("schema_version"))
    # 时间轴连续性自检
    prev = 0
    gaps = []
    for x in segs:
        if x.get("start_minute") != prev:
            gaps.append(f"{prev}->{x.get('start_minute')}")
        prev = x.get("end_minute")
    print("时间轴断点:", gaps or "无", "| 覆盖到:", prev)
else:
    print("错误:", d.get("error"))
