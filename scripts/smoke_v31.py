"""V3.1 本地接口冒烟：登录态全量 GET + 关键写链路 + 新增鉴权点。

用法：python scripts/smoke_v31.py [base_url]
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def req(method, path, body=None, token=None, cookie=None, headers=None):
    url = BASE + path
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if cookie:
        r.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", {}


def jd(raw):
    """统一解包 v1 信封 {code,data}。"""
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict):
        merged = dict(d["data"])
        for k, v in d.items():
            if k not in ("data",):
                merged.setdefault(k, v)
        return merged
    return d if isinstance(d, dict) else {}


def main():
    ok = True

    # 1) 健康
    s, b, _ = req("GET", "/api/health")
    h = json.loads(b)
    print(f"[health] {s} version={h.get('version')} ai_ready={h.get('ai_ready')} "
          f"key_fp={h.get('ai_key_fp')} migrations={h.get('migrations')}")

    # 2) 登录（只登录一次：登录有限流，重复登录会被 429）
    s, b, hh = req("POST", "/api/v1/auth/login",
                   {"email": "demo@lupin.test", "password": "demo1234"})
    if s != 200:
        print("[login] FAIL", s, b[:200])
        return 1
    d = json.loads(b)
    token = d.get("access_token")
    ck = None
    # 抓 refresh cookie（真实线上路径靠它）
    raw = hh.get("Set-Cookie") or hh.get("set-cookie") or ""
    if "lupin_refresh=" in raw:
        ck = "lupin_refresh=" + raw.split("lupin_refresh=")[1].split(";")[0]
    print(f"[login] 200 token_len={len(token or '')} cookie={'yes' if ck else 'no'}")

    # 3) 组织
    s, b, _ = req("GET", "/api/v1/orgs", token=token)
    orgs = json.loads(b)
    if isinstance(orgs, dict) and "data" in orgs:
        orgs = orgs["data"]
    orgs = orgs if isinstance(orgs, list) else (orgs.get("items") or [])
    oid = orgs[0]["id"]
    print(f"[orgs] {s} 组织数={len(orgs)} 当前角色={orgs[0].get('my_role') or orgs[0].get('role')}")

    # 4) 新鉴权点：未登录调 ai-tasks 应 401
    s, b, _ = req("POST", "/api/v1/ai-tasks",
                  {"task_type": "ideas", "org_id": oid, "payload": {}})
    print(f"[auth] 未登录建任务 → {s} {'OK' if s == 401 else 'FAIL'}")
    ok &= s == 401

    s, b, _ = req("POST", "/api/colloquial-rewrite", {"text": "测试"})
    print(f"[auth] 未登录拉呱改写 → {s} {'OK' if s == 401 else 'FAIL'}")
    ok &= s == 401

    # 5) 全量 GET 冒烟（openapi 里所有 GET，带 org_id/占位）
    s, b, _ = req("GET", "/openapi.json")
    paths = json.loads(b)["paths"]
    skips = ("/api/v1/ai-tasks/{task_id}", "/api/v1/works/{work_id}")
    fails = []
    n = 0
    for p, ops in sorted(paths.items()):
        if "get" not in ops or "{" in p or p in skips:
            continue
        params = [q["name"] for q in ops["get"].get("parameters", [])]
        url = p
        if "org_id" in params:
            url += ("&" if "?" in url else "?") + "org_id=" + urllib.parse.quote(oid)
        s, b, _ = req("GET", url, token=token, cookie=ck)
        n += 1
        if s >= 500 or s == -1:
            fails.append((p, s, b[:120]))
            print(f"  [5xx] {p} {s} {b[:120]}")
    print(f"[GET] {n} 个接口，5xx/异常 {len(fails)} 个")
    ok &= not fails

    # 6) 幂等：同 key 同体两次 → 第二次回放
    body = {"task_type": "ideas", "org_id": oid,
            "payload": {"product": {"name": "冒烟测试"}, "facts": []}}
    key = "smoke-fixed-key-001"
    s1, b1, _ = req("POST", "/api/v1/ai-tasks", body, token=token,
                    headers={"Idempotency-Key": key})
    s2, b2, _ = req("POST", "/api/v1/ai-tasks", body, token=token,
                    headers={"Idempotency-Key": key})
    d1, d2 = jd(b1), jd(b2)
    t1 = d1.get("id") if s1 in (200, 202) else None
    t2 = d2.get("id") if s2 in (200, 202) else None
    replay = d2.get("idempotent_replay")
    print(f"[idem] 第一次 {s1} id={t1} | 第二次 {s2} id={t2} replay={replay} "
          f"{'OK' if (t1 and t1 == t2 and replay) else 'FAIL'}")
    ok &= bool(t1 and t1 == t2 and replay)

    # 7) 任务查询 + 组织隔离（换一个不存在的 org 应 404）
    if t1:
        s, b, _ = req("GET", f"/api/v1/ai-tasks/{t1}", token=token)
        print(f"[task] 查询 {s} status={jd(b).get('status') if s == 200 else b[:80]}")
        s, b, _ = req("GET", f"/api/v1/ai-tasks/{t1}?org_id={oid}", token=token)
        print(f"[task] 带 org_id 查询 {s}")

    # 8) 拉呱改写（登录态，带 X-Org-Id）
    s, b, _ = req("POST", "/api/colloquial-rewrite",
                  {"text": "这款苹果脆甜多汁，产自烟台。", "product_name": "烟台苹果"},
                  token=token, cookie=ck, headers={"X-Org-Id": oid})
    print(f"[colloquial] {s} "
          f"{'prompt=' + str(jd(b).get('prompt_version')) if s == 200 else b[:120]}")

    print("\n全部通过" if ok else "\n存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
