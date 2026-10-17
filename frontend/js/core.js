/* 鲁品开拍 - 前端逻辑（原生 JS） */
const state = { product: null, facts: [], factsUpdatedAt: 0, ideas: [], ideasFactsHash: "", direction: "", storyboard: null, sbFactsHash: "", risks: [], locked: new Set() };
const $ = (id) => document.getElementById(id);
const SHOT_TYPES = ["特写", "近景", "中景", "中远景", "全景", "远景"];

function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(id).classList.add("active");
  document.querySelectorAll(".step").forEach((s) => s.classList.toggle("active", s.dataset.view === id));
  window.scrollTo(0, 0);
  if (typeof renderStageProgress === "function") renderStageProgress(id);
}
document.querySelectorAll(".step").forEach((s) => s.addEventListener("click", () => showView(s.dataset.view)));

function setLoading(text) {
  $("loading_text").textContent = text || "处理中…";
  $("loading").classList.toggle("hidden", !text);
}
function showErr(id, msg) {
  const el = $(id);
  el.textContent = msg || "";
  el.classList.toggle("show", !!msg);
}
async function api(path, body, opts) {
  opts = opts || {};
  const headers = { "Content-Type": "application/json" };
  // 用户自定义 Key 通过请求头透传（仅内存，不持久化）
  const cfg = getApiConfig();
  if (cfg.key) headers["X-Lupin-Key"] = cfg.key;
  if (cfg.base) headers["X-Lupin-Base"] = cfg.base;
  if (cfg.model) headers["X-Lupin-Model"] = cfg.model;
  const r = await fetch(path, {
    method: "POST", headers,
    body: JSON.stringify(body || {}),
    signal: opts.signal || undefined, // S7.4：调用方可经 WorkGuard 传入 signal
  });
  const data = await r.json().catch(() => ({}));
  // TODO(028)：v1 接口失败时 data.code 为业务错误码（如 1001 作品不存在、
  // 4090 版本冲突），前端应按码展示文案；老 /api/* 接口保持 data.detail 兼容。
  // S7：错误对象携带 status/code/payload，供 LPConflict.isConflict 等识别。
  if (!r.ok) {
    const err = new Error(data.message || data.detail || ("请求失败 HTTP " + r.status));
    err.status = r.status;
    err.code = data.code;
    err.payload = data;
    throw err;
  }
  return data;
}
const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/* S7.4：作品相关请求的 AbortSignal。切换作品时 WorkGuard.abortAll() 会取消
   未完成的旧请求；响应回来后调用方还应校验 work 归属（见 015）。 */
function workSignal() {
  try { return (window.WorkGuard && window.WorkGuard.signal()) || undefined; }
  catch (e) { return undefined; }
}
function isAbortError(e) {
  return !!(e && (e.name === "AbortError" || /abort/i.test(String(e.message || ""))));
}

/* 016: 统一前端状态更新入口。
   用法：updateState((st) => { st.risks[i].confirmed = true; });
   自动触发：持久化保存 + 可选的关联失效（分镜变化 → 可行性过期检查）。
   opts.storyboardChanged=true 时，分镜内容变了，提示可行性报告可能过期。 */
function updateState(mutator, opts) {
  opts = opts || {};
  mutator(state);
  if (opts.storyboardChanged && typeof checkFeasibilityStale === "function") {
    setTimeout(checkFeasibilityStale, 100);
  }
  if (typeof markDirty === "function") markDirty();
  return state;
}


/* ---------- 用户 API 配置（V6/部署） ---------- */
// Key 仅存 sessionStorage（标签页关闭即清除），不写入 localStorage
function getApiConfig() {
  try {
    return {
      base: sessionStorage.getItem("lupin_api_base") || "",
      key: sessionStorage.getItem("lupin_api_key") || "",
      model: sessionStorage.getItem("lupin_api_model") || "",
    };
  } catch (e) { return { base: "", key: "", model: "" }; }
}

function setApiConfig(base, key, model) {
  try {
    if (base) sessionStorage.setItem("lupin_api_base", base);
    else sessionStorage.removeItem("lupin_api_base");
    if (key) sessionStorage.setItem("lupin_api_key", key);
    else sessionStorage.removeItem("lupin_api_key");
    if (model) sessionStorage.setItem("lupin_api_model", model);
    else sessionStorage.removeItem("lupin_api_model");
    return true;
  } catch (e) { return false; }
}

function clearApiConfig() {
  try {
    sessionStorage.removeItem("lupin_api_base");
    sessionStorage.removeItem("lupin_api_key");
    sessionStorage.removeItem("lupin_api_model");
  } catch (e) {}
}

function updateCfgStatus() {
  const cfg = getApiConfig();
  const el = $("cfg_status");
  if (!el) return;
  if (cfg.key) {
    el.textContent = "✓ 已配置自定义 Key（仅当前标签页有效）";
    el.className = "";
  } else {
    el.textContent = "当前使用服务默认配置（如未配置，需填写 Key 才能使用 AI 功能）";
    el.className = "muted";
  }
}
