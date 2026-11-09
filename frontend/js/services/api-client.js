/* 鲁品开拍 - 统一 API 客户端（S7.2）
   用法：const r = await ApiClient.get("/api/v1/works");
         const r = await ApiClient.post("/api/v1/works", body, { idempotencyKey });
   - 统一 base URL / X-Request-ID / 超时 / 错误解析
   - v1 信封 {code, message, details} 与老格式 {detail} 统一解析为 ApiError
   - GET 自动重试 1 次；写操作不自动重试（调用方可传 idempotencyKey）
   - 401 走 AuthClient 刷新 token 后重试一次；403 抛权限错误
   - 安全：绝不把 API Key 写入 localStorage / sessionStorage / console
*/
(function () {
  "use strict";

  var DEFAULT_TIMEOUT_MS = 30000;
  // AI_TIMEOUT_MS 对齐后端单次调用墙钟（ai_service.SINGLE_CALL_TIMEOUT=300s）：
  // 此前 120s 仍短于后端整任务 600s，且 /api/v1/plan 不在白名单里，
  // 创意策划 POST /api/v1/plan/tasks/{id}/generate 只拿到 30s 就被前端 abort，
  // 截图里的“请求超时”就是这么来的。超过 300s 仍超时时，靠后端幂等回放，
  // 重试一次（同请求体、同幂等键）即可拿到已完成的结果，不要连续狂点。
  var AI_TIMEOUT_MS = 300000;
  // AI 相关路径走更长的超时
  var AI_PATH_HINTS = ["/api/ideas", "/api/storyboard", "/api/regenerate-shot",
    "/api/risk-check", "/api/feasibility-check", "/api/v1/ai-tasks",
    "/api/v1/plan", "/api/colloquial-rewrite"];

  function ApiError(status, code, message, details, requestId) {
    this.name = "ApiError";
    this.status = status;
    this.code = code || 0;
    this.message = message || ("请求失败 HTTP " + status);
    this.details = details || null;
    this.requestId = requestId || "";
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function genRequestId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function timeoutFor(path) {
    for (var i = 0; i < AI_PATH_HINTS.length; i++) {
      if (path.indexOf(AI_PATH_HINTS[i]) === 0) return AI_TIMEOUT_MS;
    }
    return DEFAULT_TIMEOUT_MS;
  }

  function parseError(status, data, requestId) {
    // v1 信封：{code, message, details}；老接口：{detail}
    var code = data && typeof data.code === "number" ? data.code : 0;
    var message = (data && (data.message || data.detail)) || ("请求失败 HTTP " + status);
    var details = (data && data.details) || null;
    return new ApiError(status, code, message, details, requestId);
  }

  // 用户自定义模型 Key：只从内存取，绝不持久化（core.js 的 getApiConfig 只读 sessionStorage）
  function userKeyHeaders(headers) {
    try {
      if (typeof getApiConfig === "function") {
        var cfg = getApiConfig();
        if (cfg.key) headers["X-Lupin-Key"] = cfg.key;
        if (cfg.base) headers["X-Lupin-Base"] = cfg.base;
        if (cfg.model) headers["X-Lupin-Model"] = cfg.model;
      }
    } catch (e) { /* 忽略，降级为无 Key */ }
    return headers;
  }

  function authHeaders(headers) {
    try {
      if (typeof AuthClient === "object" && AuthClient.getAccessToken) {
        var t = AuthClient.getAccessToken();
        if (t) headers["Authorization"] = "Bearer " + t;
      }
    } catch (e) { /* 忽略 */ }
    return headers;
  }

  // V3.1 P0 修正：原实现直接读 app.js 里的 `S`，但 app.js 与 api-client.js
  // 各自包在独立 IIFE 内，`typeof S` 恒为 "undefined" —— X-Org-Id 与 ?org_id=
  // 两条兜底从未生效过，组织预算与事实 grounding 静默失效。
  // 改为本文件自持一份组织注册表：app.js 登录/切换组织后调用 ApiClient.setOrg(id)，
  // 同时落 localStorage，刷新页面在 ensureOrg() 跑完之前也能带上。
  var ORG_STORE_KEY = "lupin_org_id";
  var _orgId = "";
  function setOrg(orgId) {
    _orgId = orgId ? String(orgId) : "";
    try {
      if (_orgId) localStorage.setItem(ORG_STORE_KEY, _orgId);
      else localStorage.removeItem(ORG_STORE_KEY);
    } catch (e) { /* 隐私模式下 localStorage 可能抛错，忽略 */ }
  }
  function currentOrgId() {
    if (_orgId) return _orgId;
    try {
      var v = localStorage.getItem(ORG_STORE_KEY);
      if (v) { _orgId = v; return v; }
    } catch (e) { /* 忽略 */ }
    // 兜底：若 app.js 将来把 S 挂到 window 上，仍能取到
    try {
      if (typeof S === "object" && S && S.org && S.org.id) return S.org.id;
    } catch (e) { /* 忽略 */ }
    return "";
  }

  // V3.1 P0：AI 网关经 X-Org-Id 头取组织做预算；登录后自动带上
  function orgHeaders(headers) {
    var oid = currentOrgId();
    if (oid) headers["X-Org-Id"] = oid;
    return headers;
  }

  // V3.1 P0 修正：幂等键必须"同一次用户意图复用同一个 key"，否则服务端
  // 的 (route,key) 去重永远命中不了——原实现每次请求都生成新 UUID，
  // 双击照样建两个任务、扣两次费。
  // 这里传 idempotencyKey:"auto" 即按 请求方法+路径+请求体 做 2 秒窗口内的
  // 复用：双击/重试落在同一窗口 → 同一个 key；真正的新一次操作（改了参数，
  // 或过了窗口）→ 新 key，不会被误判成重复。
  var IDEM_REUSE_MS = 2000;
  var _recentIdem = [];
  function autoIdempotencyKey(method, path, body) {
    var sig;
    try { sig = method + "\n" + path + "\n" + JSON.stringify(body === undefined ? null : body); }
    catch (e) { sig = method + "\n" + path + "\n" + String(body); }
    var now = Date.now();
    _recentIdem = _recentIdem.filter(function (x) { return now - x.at < IDEM_REUSE_MS; });
    for (var i = 0; i < _recentIdem.length; i++) {
      if (_recentIdem[i].sig === sig) return _recentIdem[i].key;
    }
    var key;
    try {
      key = (typeof crypto !== "undefined" && crypto.randomUUID)
        ? crypto.randomUUID()
        : "idem-" + now.toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    } catch (e) {
      key = "idem-" + now.toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    }
    _recentIdem.push({ sig: sig, key: key, at: now });
    if (_recentIdem.length > 20) _recentIdem.shift();
    return key;
  }

  // 生产网关会剥离自定义请求头（X-Org-Id 到不了服务端），预算/事实 grounding
  // 就会静默失效。这里给同步 AI 接口额外带一份 ?org_id=，服务端两者取其一。
  var ORG_QUERY_PATHS = [
    "/api/ideas", "/api/storyboard", "/api/regenerate-shot", "/api/risk-check",
    "/api/feasibility-check", "/api/v2/feasibility-check",
    "/api/colloquial-rewrite", "/api/delivery-check"
  ];
  function withOrgQuery(path) {
    try {
      if (!path || path.indexOf("org_id=") >= 0) return path;
      var bare = String(path).split("?")[0];
      if (ORG_QUERY_PATHS.indexOf(bare) < 0) return path;
      var oid = currentOrgId();
      if (!oid) return path;
      return path + (String(path).indexOf("?") >= 0 ? "&" : "?") +
        "org_id=" + encodeURIComponent(oid);
    } catch (e) { return path; }
  }

  async function doFetch(method, path, body, opts) {
    opts = opts || {};
    var reqPath = withOrgQuery(path);
    var requestId = opts.requestId || genRequestId();
    var headers = { "Content-Type": "application/json", "X-Request-ID": requestId };
    if (opts.idempotencyKey) {
      headers["Idempotency-Key"] = (opts.idempotencyKey === "auto")
        ? autoIdempotencyKey(method, reqPath, body)
        : opts.idempotencyKey;
    }
    headers = authHeaders(orgHeaders(userKeyHeaders(headers)));

    var controller = null, timer = null;
    if (typeof AbortController !== "undefined") {
      controller = new AbortController();
      timer = setTimeout(function () { controller.abort(); },
        opts.timeoutMs || timeoutFor(reqPath));
    }
    try {
      var r = await fetch(reqPath, {
        method: method,
        headers: headers,
        credentials: "include", // refresh token 在 httpOnly Cookie 里
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller ? controller.signal : undefined,
      });
      var data = await r.json().catch(function () { return {}; });
      var respId = r.headers.get("X-Request-ID") || requestId;
      if (!r.ok) throw parseError(r.status, data, respId);
      return { data: data, requestId: respId, status: r.status };
    } catch (e) {
      if (e && e.name === "AbortError") {
        throw new ApiError(0, 0, "请求超时，请稍后重试", null, requestId);
      }
      throw e;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function request(method, path, body, opts) {
    opts = opts || {};
    var retriedAuth = false;
    var attempts = 0;
    var maxAttempts = (method === "GET" && !opts.noRetry) ? 2 : 1; // GET 安全重试 1 次
    while (true) {
      attempts++;
      try {
        return await doFetch(method, path, body, opts);
      } catch (e) {
        var isNetwork = e instanceof TypeError || (e instanceof ApiError && e.status === 0);
        // 401：尝试刷新 token 后重试一次
        if (e instanceof ApiError && e.status === 401 && !retriedAuth &&
            typeof AuthClient === "object" && AuthClient.tryRefresh) {
          retriedAuth = true;
          var ok = await AuthClient.tryRefresh();
          if (ok) continue;
          if (typeof AuthClient.notifySessionExpired === "function") AuthClient.notifySessionExpired();
          throw new ApiError(401, e.code, "登录已过期，请重新登录", e.details, e.requestId);
        }
        if (e instanceof ApiError && e.status === 403) {
          throw new ApiError(403, e.code, e.message || "没有权限执行此操作", e.details, e.requestId);
        }
        if (isNetwork && attempts < maxAttempts) continue; // 网络抖动：GET 重试
        throw e;
      }
    }
  }

  window.ApiClient = {
    ApiError: ApiError,
    get: function (path, opts) { return request("GET", path, undefined, opts); },
    post: function (path, body, opts) { return request("POST", path, body, opts); },
    patch: function (path, body, opts) { return request("PATCH", path, body, opts); },
    put: function (path, body, opts) { return request("PUT", path, body, opts); },
    del: function (path, opts) { return request("DELETE", path, undefined, opts); },
    genRequestId: genRequestId,
    DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
    AI_TIMEOUT_MS: AI_TIMEOUT_MS,
    // app.js 登录成功 / 切换组织后必须调用一次；登出传 null 清空
    setOrg: setOrg,
    getOrg: currentOrgId,
  };
})();
