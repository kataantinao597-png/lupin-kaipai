/* 鲁品开拍 - 前端逻辑（原生 JS） */
const state = { product: null, facts: [], factsUpdatedAt: 0, ideas: [], ideasFactsHash: "", direction: "", storyboard: null, sbFactsHash: "", risks: [], locked: new Set() };
const $ = (id) => document.getElementById(id);
const SHOT_TYPES = ["特写", "近景", "中景", "中远景", "全景", "远景"];

function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(id).classList.add("active");
  document.querySelectorAll(".step").forEach((s) => s.classList.toggle("active", s.dataset.view === id));
  window.scrollTo(0, 0);
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
async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  // 用户自定义 Key 通过请求头透传（仅内存，不持久化）
  const cfg = getApiConfig();
  if (cfg.key) headers["X-Lupin-Key"] = cfg.key;
  if (cfg.base) headers["X-Lupin-Base"] = cfg.base;
  if (cfg.model) headers["X-Lupin-Model"] = cfg.model;
  const r = await fetch(path, {
    method: "POST", headers,
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("请求失败 HTTP " + r.status));
  return data;
}
const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");


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
