"""线上验收：健康、登录、Cookie 回退鉴权、V3.0 新模块、受限接口。"""
import json
import urllib.error
import urllib.request

BASE = "https://lupin-kaipai.app.workbuddy.host"


def req(method, path, payload=None, token=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode(), {}


def unwrap(b):
    d = json.loads(b)
    return d.get("data") if isinstance(d, dict) and "data" in d else d


print("1) health")
s, b, _ = req("GET", "/api/health")
h = json.loads(b)
print("   ", s, "migrations=", h.get("migrations"), "ai_ready=", h.get("ai_ready"),
      "ai_key_fp=", h.get("ai_key_fp"))

print("2) 未登录访问用量接口")
for p in ("/api/v1/ai/calls", "/api/v1/ai/usage/summary"):
    s, b, _ = req("GET", p)
    print("   ", p, s, b.decode()[:80])

print("3) 登录")
s, b, hdrs = req("POST", "/api/v1/auth/login",
                 {"email": "demo@lupin.test", "password": "demo1234"})
print("   ", s, b[:60].decode())
tok = json.loads(b)["access_token"]
setc = hdrs.get("Set-Cookie", "")
cookie = setc.split(";")[0] if setc else ""
print("    cookie:", bool(cookie), cookie.split("=")[0] if cookie else "")

print("4) 仅靠 Cookie 取会话（模拟网关剥离 Authorization）")
s, b, _ = req("GET", "/api/v1/auth/me", cookie=cookie)
print("   ", s, b[:100].decode())

print("5) 组织与新模块（带 Cookie，线上网关会剥掉 Authorization）")
s, b, _ = req("GET", "/api/v1/orgs", token=tok, cookie=cookie)
orgs = unwrap(b)
if isinstance(orgs, dict):
    orgs = orgs.get("items") or orgs.get("orgs") or []
oid = orgs[0]["id"]
print("    org:", oid, orgs[0].get("my_role"))
for p in ("/api/v1/products?org_id=" + oid,
          "/api/v1/facts?org_id=" + oid,
          "/api/v1/copy/docs?org_id=" + oid,
          "/api/v1/plan/tasks?org_id=" + oid,
          "/api/v1/platforms/rules?org_id=" + oid,
          "/api/v1/publish/records?org_id=" + oid,
          "/api/v1/review/dashboard?org_id=" + oid,
          "/api/v1/asset-library/search?org_id=" + oid + "&q=",
          "/api/v1/works?org_id=" + oid):
    s, b, _ = req("GET", p, token=tok, cookie=cookie)
    flag = "OK " if 200 <= s < 300 else "!! "
    print("   %s%-58s %s %s" % (flag, p.split("?")[0], s, b[:60].decode("utf-8", "replace")))

print("6) 页面")
for p in ("/", "/app", "/login"):
    s, b, _ = req("GET", p)
    print("   ", p, s, len(b), "bytes")

print("7) OpenAPI 路径数")
s, b, _ = req("GET", "/openapi.json")
spec = json.loads(b)
print("    paths:", len(spec["paths"]))
