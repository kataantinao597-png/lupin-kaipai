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
  var AI_TIMEOUT_MS = 120000;
  // AI 相关路径走更长的超时
  var AI_PATH_HINTS = ["/api/ideas", "/api/storyboard", "/api/regenerate-shot",
    "/api/risk-check", "/api/feasibility-check", "/api/v1/ai-tasks"];

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

  async function doFetch(method, path, body, opts) {
    opts = opts || {};
    var requestId = opts.requestId || genRequestId();
    var headers = { "Content-Type": "application/json", "X-Request-ID": requestId };
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
    authHeaders(userKeyHeaders(headers));

    var controller = null, timer = null;
    if (typeof AbortController !== "undefined") {
      controller = new AbortController();
      timer = setTimeout(function () { controller.abort(); },
        opts.timeoutMs || timeoutFor(path));
    }
    try {
      var r = await fetch(path, {
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
  };
})();
