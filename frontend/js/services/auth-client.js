/* 鲁品开拍 - 认证客户端（S7.2）
   - accessToken 只存内存，刷新/关闭页面即失效；绝不写入 localStorage
   - refresh token 由后端放在 httpOnly Cookie（lupin_refresh），JS 读不到、只随请求自动带
   - 401 时由 ApiClient 调用 tryRefresh() 换新 token 并重试一次
*/
(function () {
  "use strict";

  var accessToken = null;   // 内存唯一持有
  var userInfo = null;
  var refreshing = null;    // 并发刷新去重
  var sessionExpiredHandlers = [];

  function setSession(token, user) {
    accessToken = token || null;
    userInfo = user || null;
  }

  function clearSession() {
    accessToken = null;
    userInfo = null;
  }

  async function rawPost(path, body) {
    var r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body || {}),
    });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) {
      var err = new Error(data.detail || data.message || ("请求失败 HTTP " + r.status));
      err.status = r.status;
      throw err;
    }
    return data;
  }

  async function login(email, password) {
    var data = await rawPost("/api/v1/auth/login", { email: email, password: password });
    setSession(data.access_token, data.user || null);
    return data;
  }

  async function register(email, password, displayName) {
    var body = { email: email, password: password };
    if (displayName) body.display_name = displayName;
    var data = await rawPost("/api/v1/auth/register", body);
    // 后端 register 返回用户信息（无 token），调用方需再 login
    setSession(data.access_token || null, data.user || data || null);
    return data;
  }

  async function logout() {
    try { await rawPost("/api/v1/auth/logout", {}); } catch (e) { /* 忽略 */ }
    clearSession();
  }

  // 401 时调用：用 Cookie 里的 refresh token 换新 access token。并发只发一次。
  async function tryRefresh() {
    if (refreshing) return refreshing;
    refreshing = (async function () {
      try {
        var data = await rawPost("/api/v1/auth/refresh", {});
        setSession(data.access_token, data.user || userInfo);
        return true;
      } catch (e) {
        clearSession();
        return false;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  async function me() {
    if (!accessToken) return null;
    try {
      var r = await fetch("/api/v1/auth/me", {
        headers: { "Authorization": "Bearer " + accessToken },
        credentials: "include",
      });
      if (!r.ok) return null;
      userInfo = await r.json();
      return userInfo;
    } catch (e) {
      return null;
    }
  }

  function onSessionExpired(fn) {
    if (typeof fn === "function") sessionExpiredHandlers.push(fn);
  }

  // ApiClient 401 刷新失败后调用
  function handleSessionExpired() {
    clearSession();
    sessionExpiredHandlers.forEach(function (fn) {
      try { fn(); } catch (e) { /* 忽略 */ }
    });
  }

  window.AuthClient = {
    login: login,
    register: register,
    logout: logout,
    me: me,
    tryRefresh: tryRefresh,
    getAccessToken: function () { return accessToken; },
    getUser: function () { return userInfo; },
    isLoggedIn: function () { return !!accessToken; },
    onSessionExpired: onSessionExpired,       // 注册"会话过期"回调
    notifySessionExpired: handleSessionExpired, // 供 ApiClient：刷新失败后触发
  };
})();
