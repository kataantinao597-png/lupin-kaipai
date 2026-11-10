/* 鲁品开拍 · 新版前端应用逻辑（v3 设计稿 + 真实接口）
   - 认证走 AuthClient（accessToken 只存内存）；接口走 ApiClient（统一超时/401刷新/信封解析）
   - v1 信封 {code,message,data} 用 V1() 解包；老 /api/* 接口直接取 r.data
   - 作品数据持久化走服务端 PATCH /api/v1/works/{id}（乐观锁 version）
*/
(function () {
"use strict";

/* ---------- 基础 ---------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const V1 = (r) => (r && r.data && r.data.data !== undefined) ? r.data.data : (r ? r.data : null);
const RAW = (r) => (r ? r.data : null);

let toastTimer = null;

/* ---------- Phase4 非遗深化：团花线稿（复用 v0 剪纸团花 #pf 的几何路径） ---------- */
const FLOWER_SVG = `<svg viewBox="0 0 100 100" fill="none" aria-hidden="true"><circle cx="50" cy="50" r="45" stroke="#C29C5F" stroke-width="2.5" stroke-dasharray="7 6"/><g stroke="#C62828" stroke-width="2.5"><ellipse cx="50" cy="25" rx="11" ry="24"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(45 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(90 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(135 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(180 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(225 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(270 50 50)"/><ellipse cx="50" cy="25" rx="11" ry="24" transform="rotate(315 50 50)"/><circle cx="50" cy="50" r="9"/><circle cx="50" cy="50" r="3" fill="#C62828" stroke="none"/></g></svg>`;

/* 落地批点缀（Luna gpt-6-luna 生成、自筛后采用）：空相框 + 两叶芽线稿，素材为空时用它替代团花——语义是「空框待收」 */
const FRAME_SVG = `<svg viewBox="0 0 120 90" fill="none" aria-hidden="true"><g fill="none" stroke="#2E7D6B" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M24 18 L91 15 L94 67 L27 70 Z"/><path d="M32 25 L84 23"/><path d="M77 66 C77 61 78 56 81 51"/><path d="M79 58 C73 57 71 53 71 50 C76 50 79 53 79 58 Z"/><path d="M80 55 C82 49 86 47 89 48 C88 53 85 56 80 55 Z"/><path d="M11 76 L33 76"/></g></svg>`;

/* 空状态渲染助手：团花线稿 + 引导文字 + 可选 CTA（I-7）；报错态不走这里（保持现有 err 样式）
   action:{label, fn} —— fn 注册进 EMPTY_ACTS，靠全局委托触发（各视图 innerHTML 重渲染后依然有效） */
const EMPTY_ACTS = [];
function emptyState(text, hint, compact, action, art) {
  let actHtml = "";
  if (action && action.label && typeof action.fn === "function") {
    const idx = EMPTY_ACTS.push(action.fn) - 1;
    actHtml = `<div class="empty-act"><button type="button" class="btn primary sm" data-empty-act="${idx}">${esc(action.label)}</button></div>`;
  }
  return `<div class="empty-state${compact ? " compact" : ""}${art ? " has-art" : ""}" role="status">${art || FLOWER_SVG}<div class="empty-tx"><p class="empty-text">${esc(text)}</p>${hint ? `<p class="empty-hint">${esc(hint)}</p>` : ""}${actHtml}</div></div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-empty-act]");
  if (!b) return;
  const fn = EMPTY_ACTS[+b.dataset.emptyAct];
  if (fn) fn();
});

/* 落印反馈：在关键确认成功时于卡片角落盖一枚小印；锚点找不到则落到右下角兜底 */
function stampPop(anchor, text) {
  const s = document.createElement("span");
  s.className = "stamp-pop";
  s.textContent = text;
  s.setAttribute("aria-hidden", "true");
  if (anchor && anchor.isConnected) {
    if (getComputedStyle(anchor).position === "static") anchor.style.position = "relative";
    anchor.appendChild(s);
  } else {
    s.classList.add("fixed");
    document.body.appendChild(s);
  }
  setTimeout(() => s.remove(), 2000);
}
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}
function todo(name) { toast(`「${name}」功能待接入，当前版本暂不支持`); }

function aiLoading(text) {
  $("ai_loading_text").textContent = text || "AI 生成中…";
  $("ai_loading").classList.add("show");
}
function aiLoadingDone() { $("ai_loading").classList.remove("show"); }

function errText(id, msg) { const el = $(id); if (el) el.textContent = msg || ""; }

/* ---------- 通用弹窗（Phase2：替代原生 prompt/confirm/alert，复用 .modal-mask 体系） ---------- */
/* 焦点圈定：Tab 只在弹窗内循环（I-6） */
function _trapTab(m, e) {
  const f = Array.from(m.querySelectorAll("button, input, textarea, select, [tabindex]")).filter((el) => !el.disabled && el.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
function _openUiModal(html, label) {
  const prev = $("ui_modal");
  if (prev) { // 顶掉旧弹窗前先结算它，避免旧调用方的 await 永久挂起（I-6）
    if (typeof prev._settle === "function") prev._settle();
    if (prev.isConnected) prev.remove();
  }
  const m = document.createElement("div");
  m.className = "modal-mask"; m.id = "ui_modal";
  m._returnFocus = document.activeElement;
  m.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${esc(label || "弹窗")}">${html}</div>`;
  document.body.appendChild(m);
  m.classList.add("show");
  return m;
}
function _closeUiModal(m) {
  const t = m._returnFocus;
  m.remove();
  if (t && t.isConnected && typeof t.focus === "function") t.focus({ preventScroll: true });
}
/* 确认弹窗：resolve(true)=点确定，false=取消/Esc/点遮罩 */
function modalConfirm(opts) {
  const o = opts || {};
  const title = o.title || "请确认", body = o.body || "";
  const okText = o.okText || "确定", cancelText = o.cancelText || "取消";
  return new Promise((resolve) => {
    const danger = !!o.danger; // B-1：危险确认——红按钮 + 默认焦点在「取消」
    const m = _openUiModal(`
      <h3>${esc(title)}</h3>
      ${body ? `<div style="white-space:pre-line;font-size:13.5px;color:var(--ink2);margin-bottom:18px">${esc(body)}</div>` : ""}
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn ghost" data-m="cancel">${esc(cancelText)}</button>
        <button class="btn ${danger ? "danger-solid" : "primary"}" data-m="ok">${esc(okText)}</button>
      </div>`, title);
    let settled = false;
    const done = (v) => { if (settled) return; settled = true; document.removeEventListener("keydown", onKey, true); _closeUiModal(m); resolve(v); };
    m._settle = () => done(false);
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(false); }
      else if (e.key === "Tab") _trapTab(m, e);
    };
    m.addEventListener("click", (e) => { if (e.target === m) done(false); });
    m.querySelector('[data-m="cancel"]').addEventListener("click", () => done(false));
    m.querySelector('[data-m="ok"]').addEventListener("click", () => done(true));
    document.addEventListener("keydown", onKey, true);
    const focusBtn = m.querySelector(danger ? '[data-m="cancel"]' : '[data-m="ok"]');
    if (focusBtn) focusBtn.focus();
  });
}
/* 输入弹窗：resolve(字符串)=点确定，null=取消/Esc/点遮罩（与 prompt 返回值语义一致）；required 时空值不关闭 */
function modalInput(opts) {
  const o = opts || {};
  const title = o.title || "请输入", body = o.body || "";
  const okText = o.okText || "确定", required = !!o.required;
  const field = o.multiline
    ? `<textarea id="ui_modal_input" rows="4" placeholder="${esc(o.placeholder || "")}">${esc(o.value || "")}</textarea>`
    : `<input id="ui_modal_input" placeholder="${esc(o.placeholder || "")}" value="${esc(o.value || "")}">`;
  return new Promise((resolve) => {
    const m = _openUiModal(`
      <h3>${esc(title)}</h3>
      ${body ? `<div style="font-size:13.5px;color:var(--ink2);margin-bottom:10px">${esc(body)}</div>` : ""}
      <div>${field}</div>
      <div class="err-text" id="ui_modal_err" style="min-height:20px;margin-top:4px"></div>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn ghost" data-m="cancel">取消</button>
        <button class="btn primary" data-m="ok">${esc(okText)}</button>
      </div>`, title);
    const input = m.querySelector("#ui_modal_input");
    let settled = false;
    const done = (v) => { if (settled) return; settled = true; document.removeEventListener("keydown", onKey, true); _closeUiModal(m); resolve(v); };
    m._settle = () => done(null);
    const submit = () => {
      if (required && !input.value.trim()) {
        m.querySelector("#ui_modal_err").textContent = "此项必填";
        input.focus();
        return;
      }
      done(input.value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
      else if (e.key === "Tab") _trapTab(m, e);
      else if (e.key === "Enter" && !o.multiline && document.activeElement === input) { e.preventDefault(); submit(); }
    };
    m.addEventListener("click", (e) => { if (e.target === m) done(null); });
    m.querySelector('[data-m="cancel"]').addEventListener("click", () => done(null));
    m.querySelector('[data-m="ok"]').addEventListener("click", submit);
    document.addEventListener("keydown", onKey, true);
    input.focus();
    if (!o.multiline) input.select();
  });
}
/* 信息弹窗（替代 alert）：单按钮，Esc/点遮罩同样关闭 */
function modalInfo(opts) {
  const o = opts || {};
  const title = o.title || "提示", body = o.body || "";
  return new Promise((resolve) => {
    const m = _openUiModal(`
      <h3>${esc(title)}</h3>
      <div style="white-space:pre-line;font-size:13.5px;color:var(--ink2);margin-bottom:18px;max-height:50vh;overflow:auto">${esc(body)}</div>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn primary" data-m="ok">${esc(o.okText || "知道了")}</button>
      </div>`, title);
    let settled = false;
    const done = () => { if (settled) return; settled = true; document.removeEventListener("keydown", onKey, true); _closeUiModal(m); resolve(); };
    m._settle = () => done();
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(); }
      else if (e.key === "Tab") _trapTab(m, e);
    };
    m.addEventListener("click", (e) => { if (e.target === m) done(); });
    m.querySelector('[data-m="ok"]').addEventListener("click", done);
    document.addEventListener("keydown", onKey, true);
    const okBtn = m.querySelector('[data-m="ok"]');
    if (okBtn) okBtn.focus();
  });
}
/* B-2：给 div 卡片补键盘可达——Enter/Space 触发其已有 click 逻辑（不另写处理器） */
function keyActivate(el) {
  if (!el || el.dataset.kbBound) return;
  el.dataset.kbBound = "1";
  el.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === el) { e.preventDefault(); el.click(); }
  });
}

/* ---------- I-6：顶栏全局搜索（作品/产品前端过滤，数据已在内存 S.works/S.products） ---------- */
function initGlobalSearch() {
  const input = $("global_search"), box = $("search_results");
  if (!input || !box) return;
  let items = []; // [{type:"work"|"product", id, name}]
  let active = -1;
  const close = () => { box.classList.remove("show"); box.innerHTML = ""; items = []; active = -1; input.setAttribute("aria-expanded", "false"); };
  const choose = (i) => {
    const it = items[i];
    if (!it) return;
    close();
    input.value = "";
    input.blur();
    if (it.type === "work") openWork(it.id, "v1");
    else { S.selectedProductId = it.id; go("v2"); } // v2 进入时按 selectedProductId 渲染选择器并筛选事实
  };
  const render = () => {
    if (!items.length) {
      box.innerHTML = '<div class="sr-empty">无匹配的作品或产品</div>';
    } else {
      box.innerHTML = items.map((it, i) => `
        <button type="button" class="sr-item${i === active ? " active" : ""}" role="option" aria-selected="${i === active}" data-i="${i}">
          <span class="pill ${it.type === "work" ? "blue" : "green"}">${it.type === "work" ? "作品" : "产品"}</span>
          <span class="sr-name">${esc(it.name)}</span>
        </button>`).join("");
      box.querySelectorAll(".sr-item").forEach((el) => el.addEventListener("mousedown", (e) => { e.preventDefault(); choose(+el.dataset.i); }));
    }
    box.classList.add("show");
    input.setAttribute("aria-expanded", "true");
  };
  const search = () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { close(); return; }
    const works = (S.works || []).filter((w) => (w.title || "").toLowerCase().includes(q)).slice(0, 6)
      .map((w) => ({ type: "work", id: w.id, name: w.title || "未命名作品" }));
    const prods = (S.products || []).filter((p) => (p.name || "").toLowerCase().includes(q)).slice(0, 6)
      .map((p) => ({ type: "product", id: p.id, name: p.name }));
    items = works.concat(prods);
    active = items.length ? 0 : -1;
    render();
  };
  input.addEventListener("input", search);
  input.addEventListener("focus", () => { if (input.value.trim()) search(); });
  input.addEventListener("keydown", (e) => {
    if (!box.classList.contains("show")) return;
    if (e.key === "ArrowDown") { e.preventDefault(); if (items.length) { active = (active + 1) % items.length; render(); } }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (items.length) { active = (active - 1 + items.length) % items.length; render(); } }
    else if (e.key === "Enter") { if (active >= 0) { e.preventDefault(); choose(active); } }
    else if (e.key === "Escape") { close(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) close(); });
}

/* V2.1 #3：生成类按钮请求期间禁用（防双击重复扣费），完成后恢复 */
function setBtnsDisabled(ids, disabled) {
  (ids || []).forEach((id) => { const el = $(id); if (el) el.disabled = !!disabled; });
}
function newIdempotencyKey() {
  /* V3.1 修正：不再在这里生成一次性 UUID——每次点都换 key，服务端 (route,key)
     去重永远命中不了，双击照样建两个任务。
     改由 api-client 在 "auto" 模式下按「方法+路径+请求体」做 2 秒窗口复用。 */
  return "auto";
}

/* S3 修复：自带 Key 只允许发往可信域名。白名单：deepkey.top、
   api.openai.com 及其子域名。校验放在读取侧（getApiConfig）而非仅
   保存侧：sessionStorage 是同源全局键，任何路径写入的旧值/脏值
   （含白名单实施前已存的恶意 base）都必须在真正发 Key 之前被拦下；
   只在 saveCfg 校验等于只锁前门。 */
const TRUSTED_API_HOSTS = ["deepkey.top", "api.openai.com"];
function _apiHostAllowed(base) {
  try {
    const h = new URL(base).hostname.toLowerCase();
    return TRUSTED_API_HOSTS.some(d => h === d || h.endsWith("." + d));
  } catch (e) { return false; }
}
/* 用户自带 Key（只存 sessionStorage，关闭标签页清除）——ApiClient 会自动读取 */
function getApiConfig() {
  try {
    const base = sessionStorage.getItem("lupin_api_base") || "";
    const key = sessionStorage.getItem("lupin_api_key") || "";
    const model = sessionStorage.getItem("lupin_api_model") || "";
    // 发送侧二次校验：非白名单 base 一律视为未配置，Key 绝不发出。
    // 即使旧会话里残留恶意 base（白名单实施前写入、或绕过 saveCfg 写入），
    // 到真正调用时仍会被拦下；同时清掉脏值，避免下次继续命中。
    if (base && !_apiHostAllowed(base)) {
      try {
        sessionStorage.removeItem("lupin_api_base");
        sessionStorage.removeItem("lupin_api_key");
        sessionStorage.removeItem("lupin_api_model");
      } catch (e) { /* 忽略 */ }
      return { base: "", key: "", model: "" };
    }
    return { base, key, model };
  } catch (e) { return { base: "", key: "", model: "" }; }
}
window.getApiConfig = getApiConfig;

/* ---------- 全局状态 ---------- */
const S = {
  user: null,
  org: null,
  works: [],
  activeWork: null,   // {id,title,version,status,...}
  data: null,         // activeWork.data：{product,storyboard,shoot_state,feasibility,risk,ideas,direction,poster,ideasFactsHash,sbFactsHash,shoot_note}
  facts: [],
  products: [],           // V3.0 B-3：产品档案列表（组织级）
  selectedProductId: "",  // V3.0 B-3：当前选中的产品档案 id（空=不筛选）
  genProductId: "",       // V3.0 B-4：v3 生成时关联的产品档案 id（空=不关联）
  productGenRef: null,    // V3.0 B-4/B-5：生成时记录的产品引用 {product_id,product_version,product_code,product_name}
  workFilter: "all",
  authMode: "login",
  ideaSel: -1,
};

function blankData() {
  return { product: null, storyboard: null, shoot_state: {}, feasibility: null, risk: null,
           ideas: [], direction: "", poster: null, ideasFactsHash: "", sbFactsHash: "", shoot_note: "",
           colloq_undo: [] }; // V3.1 §4.2：拉呱版采用撤销栈（原文快照）
}

/* ---------- 路由（含登录守卫） ---------- */
const NEED_AUTH = ["v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "agent"];
function loggedIn() { return !!(AuthClient && AuthClient.isLoggedIn()); }

function go(v) {
  if (NEED_AUTH.includes(v) && !loggedIn()) {
    toast("请先登录");
    v = "v-login";
  }
  document.querySelectorAll(".view").forEach((s) => s.classList.remove("active"));
  const sec = $(v);
  if (sec) sec.classList.add("active");
  document.querySelectorAll(".nav-item[data-v]").forEach((b) => b.classList.toggle("active", b.dataset.v === v));
  window.scrollTo(0, 0); // P-6：高频切换滚顶改即时，避免平滑滚动的拖沓感
  if (v !== "v0" && v !== "v-login") revealIn(sec);
  onEnter(v);
}
window.go = go;

function onEnter(v) {
  if (v === "v0") renderOverview();
  else if (v === "v1") renderWorkbench();
  else if (v === "v2") { fillProductForm(); loadProducts(); } // V3.0 B-3：先加载产品档案，加载完再按筛选加载事实
  else if (v === "v3") { renderStepper(); renderIdeas(); renderShots(); updateRiskDirtyBadge(); ensureGenProductSelect(); renderPlanSection(); } // V2.1：进入分镜视图刷新标脏徽章；V3.0 B-4：v3 产品选择器；V3.0 C：内容策划面板
  else if (v === "v4") { renderPoster(); }
  else if (v === "v5") { renderShoot(); loadAssets(); fillNote(); }
  else if (v === "v6") { renderDeliveryTitles(); loadTimeline(); renderFinalArchive(); }
  else if (v === "v7") { renderCopyOpt(); }
  else if (v === "v8") { renderPublishCenter(); }
  else if (v === "v9") { renderAssetLibrary(); }
  else if (v === "agent") { enterAgentView(); }
}

/* ================= Phase 4b：AI 创作助手 =================
   真实状态机：全部状态来自轮询 GET /api/v1/agent/runs/{id}（2.5s 间隔，
   复用 pollAiTask 的轮询模式），不用固定延迟模拟完成。
   run_status: queued/running/waiting_user/partial/completed/failed/cancelled；
   idle/发送中 为客户端态；已保存 = completed 且 summary 已持久化
   （后端完成时必写 summary 到 payload 并落 assistant 消息）。 */
const AGENT_SRC_LABEL = { v0_home:"首页一句话创作", v2_product:"产品资料", v3_idea:"创意策划", v3_storyboard:"分镜编辑", v3_risk:"风险检查", v5_shoot:"拍摄执行", chat:"直接对话" };
const AGENT_SRC_VIEW = { v0_home:"v1", v2_product:"v2", v3_idea:"v3", v3_storyboard:"v3", v3_risk:"v3", v5_shoot:"v5" };
const AGENT_SRC_HINT = {
  v2_product:"请帮我基于当前产品资料，给出下一步创作建议",
  v3_idea:"请帮我基于当前创意方案继续推进",
  v3_storyboard:"请帮我检查当前分镜并给出优化建议",
  v3_risk:"请帮我处理当前风险检查中依据不足的项",
  v5_shoot:"请帮我基于当前拍摄进度安排后续计划",
};
const AGENT_TOOL_LABEL = {
  query_facts:"查事实库", query_products:"查产品档案", generate_ideas:"生成创意方案",
  generate_storyboard:"生成分镜", check_risk:"风险检查", shooting_checklist:"拍摄清单",
  rewrite_copy:"文案改写", check_delivery:"交付检查", parse_brief:"解析需求",
  patch_work:"更新作品（需确认）", get_task_status:"查询任务状态", query_usage:"查询用量",
  regenerate_shot:"重新生成镜头", delete_work:"删除作品（需确认）",
  generate_strategy:"制定营销策略", generate_platform_copy:"生成平台文案",
};
const AGENT = {
  state:"idle", taskId:null, convId:null, lastBriefText:"",
  ctx:null, timer:null, log:[], renderedConfirmFor:null, renderedResultFor:null,
  lastState:null, pollFails:0, pendingSend:"",
  cancelReq:false, startedAt:null, // V3.2：取消请求标记 + 开始时间（前端耗时反馈）
  evSeq:0, // V3.3：过程事件流已重放到的 seq
  allEvents:[], // O-01：增量轮询下本地合并的完整事件流（供侧栏“进行中步骤”推导用）
  restoreKey:"lupin_agent_run", restoredFor:null, // V3.5：刷新恢复（sessionStorage 快照 + 防重复恢复）
  nextPollAt:0, // S-03/O-07：轮询失败退避后的下次可轮询时刻（0=不退避）
};

/* V3.3：把后端过程事件流重放进聊天区。
   started → 亮"正在X…"气泡；done → 原位替换为结果摘要（找不到 running
   气泡（刷新恢复/乱序）时直接渲染完成气泡）。增量消费，幂等可重放。 */
function renderAgentEvents(st) {
  const evs = (st && st.events) || [];
  if (!evs.length) return;
  let dirty = false;
  for (const ev of evs) {
    if (ev.seq < AGENT.evSeq) continue;
    AGENT.evSeq = ev.seq + 1;
    if (ev.type === "step_started") {
      agentLog("step", { running: true, tool: ev.tool_id, id: "ev-" + ev.seq });
    } else if (ev.type === "step_done") {
      let idx = -1;
      for (let i = AGENT.log.length - 1; i >= 0; i--) {
        const x = AGENT.log[i];
        if (x.kind === "step" && x.running && x.tool === ev.tool_id) { idx = i; break; }
      }
      if (idx >= 0) {
        AGENT.log[idx] = { kind: "step", tool: ev.tool_id, running: false,
          status: ev.status, note: ev.note || "", duration_ms: ev.duration_ms };
        dirty = true;
      } else {
        agentLog("step", { running: false, tool: ev.tool_id,
          status: ev.status, note: ev.note || "", duration_ms: ev.duration_ms });
      }
    } else if (ev.type === "artifact" && (ev.items || []).length) {
      AGENT.log.push({ kind: "artifact", tool: ev.tool_id, items: ev.items });
      dirty = true;
    }
  }
  if (dirty) renderAgentLog();
}
/* V3.3：从事件流推导"正在进行中的工具"（侧栏当前步骤行） */
function agentRunningTools(st) {
  const cnt = {};
  for (const ev of (st && st.events) || []) {
    if (ev.type === "step_started") cnt[ev.tool_id] = (cnt[ev.tool_id] || 0) + 1;
    else if (ev.type === "step_done") {
      cnt[ev.tool_id] = (cnt[ev.tool_id] || 1) - 1;
      if (cnt[ev.tool_id] <= 0) delete cnt[ev.tool_id];
    }
  }
  return Object.keys(cnt);
}
const AGENT_TERMINAL = ["completed", "partial", "failed", "cancelled"];
const AGENT_STATE_META = {
  idle:["空闲","gray"], sending:["发送中","blue"], queued:["排队中","amber"],
  running:["运行中","blue"], waiting_user:["等待确认","amber"], paused:["已暂停","amber"], partial:["部分完成","amber"],
  failed:["失败","red"], cancelled:["已取消","gray"], completed:["已完成","green"], saved:["已保存","green"],
  cancelling:["取消中","amber"], // V3.2：已发出取消请求、等待当前步骤结束
};
const AGENT_STEP_META = {
  pending:["待执行","gray"], running:["执行中","blue"], succeeded:["已完成","green"],
  waiting_user:["等待确认","amber"], failed:["失败","red"], cancelled:["已取消","gray"], skipped:["已跳过","gray"],
};

function agentNormStatus(st) {
  /* V3.2：task.status 终态优先。取消/失败落库时 payload.run_status 可能
     仍停留在创建时的 queued/running（取消路径不回写 payload），
     优先信它会导致轮询永远看不到终态——点取消后 UI 一直"卡在取消中"的根因。 */
  const ts = st && st.status;
  if (ts === "cancelled") return "cancelled";
  if (ts === "failed") return "failed";
  if (ts === "succeeded") {
    const rs0 = st && st.run_status;
    return (rs0 === "partial" || rs0 === "completed") ? rs0 : "completed";
  }
  const m = { queued:"queued", running:"running", waiting_user:"waiting_user", paused:"paused", partial:"partial",
              completed:"completed", failed:"failed", cancelled:"cancelled" };
  if (st && m[st.run_status]) {
    /* 执行中：run_status 可能滞留创建初值 queued，任务实际已 running——按 running 显示 */
    if (st.run_status === "queued" && ts === "running") return "running";
    return m[st.run_status];
  }
  if (ts === "running") return "running";
  return "queued";
}
/* 已保存：completed 且 summary 已持久化（后端完成时必写） */
function agentDisplayState(st) {
  const s = agentNormStatus(st);
  return (s === "completed" && st && st.summary) ? "saved" : s;
}
function agentToolLabel(tid) { return AGENT_TOOL_LABEL[tid] || tid || "未知步骤"; }

function renderAgentPill(disp) {
  const meta = AGENT_STATE_META[disp] || AGENT_STATE_META.idle;
  const el = $("agent_status_pill");
  if (el) { el.textContent = meta[0]; el.className = "pill " + meta[1]; }
}
function setAgentButtons(disp) {
  const cancellable = ["queued", "running", "waiting_user", "paused"].includes(disp);
  const retryable = ["failed", "cancelled"].includes(disp);
  const pausable = ["queued", "running"].includes(disp);
  const resumable = disp === "paused";
  const bc = $("btn_agent_cancel"), br = $("btn_agent_retry");
  const bp = $("btn_agent_pause"), brs = $("btn_agent_resume");
  if (bc) bc.style.display = (cancellable && !AGENT.cancelReq) ? "" : "none"; // V3.2：取消中不重复可点
  if (br) br.style.display = retryable ? "" : "none";
  if (bp) bp.style.display = (pausable && !AGENT.cancelReq) ? "" : "none";
  if (brs) brs.style.display = resumable ? "" : "none";
}
/* V3.2：耗时格式化——执行中/取消中给用户明确的时间预期 */
function agentFmtElapsed(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return "";
  if (sec < 60) return sec + " 秒";
  return Math.floor(sec / 60) + " 分 " + (sec % 60) + " 秒";
}
/* V3.3：步骤耗时（毫秒）人性化显示，修"用时 0ms"的假数据观感 */
function agentFmtDur(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return "瞬间";
  if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " 秒";
  return agentFmtElapsed(Math.round(ms / 1000));
}

/* ---------- 对话区 ---------- */
function agentLog(kind, data) {
  data = data || {};
  if (data.id) {
    const i = AGENT.log.findIndex((e) => e.id === data.id);
    if (i >= 0) { AGENT.log[i] = { kind, ...data }; renderAgentLog(); return; }
  }
  AGENT.log.push({ kind, ...data });
  renderAgentLog();
}
function renderAgentLog() {
  const box = $("agent_msgs");
  if (!box) return;
  if (!AGENT.log.length) {
    box.innerHTML = `<div class="agent-msg assistant"><div class="agent-bubble">
      <b>你好，我是 AI 创作助手。</b><br>
      <span style="color:var(--ink2)">说一句话告诉我你想做什么，我会自动安排执行计划：查事实、做创意、排分镜。关键步骤（比如修改作品、删除内容）会先请你确认再执行。</span>
      <div class="agent-suggest">
        <button data-act="suggest" data-text="给当前作品做一个 60 秒带货短视频方案">60 秒带货方案</button>
        <button data-act="suggest" data-text="检查当前作品的风险项并给出修改建议">检查风险项</button>
        <button data-act="suggest" data-text="基于已确认事实生成 3 个创意方向">生成创意方向</button>
      </div></div></div>`;
    return;
  }
  box.innerHTML = AGENT.log.map((e) => {
    if (e.kind === "user") return `<div class="agent-msg user"><div class="agent-bubble">${esc(e.text)}</div></div>`;
    /* V3.6：寒暄直答气泡（assistant 样式 + 可点示例） */
    if (e.kind === "ai") return `<div class="agent-msg assistant"><div class="agent-bubble">${esc(e.text)}
      ${e.suggests ? `<div class="agent-suggest">${e.suggests.map((s) => `<button data-act="suggest" data-text="${esc(s)}">${esc(s)}</button>`).join("")}</div>` : ""}</div></div>`;
    if (e.kind === "sys") return `<div class="agent-msg sys"><span class="agent-sysline${e.live ? " live" : ""}"><span class="dot"></span>${esc(e.text)}</span></div>`;
    /* V3.3：过程步骤气泡——running 亮"正在…"，完成原位替换为结果摘要 */
    if (e.kind === "step") {
      const label = esc(agentToolLabel(e.tool));
      if (e.running) return `<div class="agent-msg sys"><span class="agent-sysline live" id="${esc(e.id || "")}"><span class="dot"></span>正在${label}…</span></div>`;
      const ok = e.status === "succeeded";
      const dur = agentFmtDur(e.duration_ms);
      const body = e.note ? esc(e.note) : (label + (ok ? " 完成" : " 失败"));
      return `<div class="agent-msg sys"><span class="agent-sysline${ok ? "" : " err"}">${ok ? "✓" : "✕"} ${body}${dur ? `<span class="dim">${dur}</span>` : ""}</span></div>`;
    }
    /* V3.4：成果卡片——每步产出内容逐条渐入（创意标题/分镜/策略要点） */
    if (e.kind === "artifact") {
      return `<div class="agent-msg sys"><div class="agent-artifact">
        <div class="t">${esc(agentToolLabel(e.tool))} · 成果</div>
        ${e.items.map((x, i) => `<div class="it" style="animation-delay:${(i * 0.12).toFixed(2)}s">${esc(x)}</div>`).join("")}
      </div></div>`;
    }
    if (e.kind === "confirm") return agentConfirmHtml(e);
    if (e.kind === "result") return agentResultHtml(e);
    if (e.kind === "error") return agentErrorHtml(e);
    return "";
  }).join("");
  box.scrollTop = box.scrollHeight;
}
function agentConfirmHtml(e) {
  const p = e.pending || {};
  /* 只有当前 run 的等待确认卡片可点；历史卡片渲染即冻结（防重渲染冲掉冻结态） */
  const active = AGENT.state === "waiting_user" && e.tid && e.tid === AGENT.taskId;
  const dis = active ? "" : " disabled";
  return `<div class="agent-msg assistant"><div class="agent-confirm">
    <h4><span class="pill amber">等待确认</span> 步骤 ${(p.step_idx || 0) + 1} 需要你的确认</h4>
    <p>将执行「${esc(agentToolLabel(p.tool_id))}」。${p.optional ? "这是一个可选步骤。" : ""}确认后才会真正执行，你可以先看清再决定。</p>
    <div class="row">
      <button class="btn primary sm" data-act="confirm-yes" data-idx="${p.step_idx || 0}" data-tid="${esc(e.tid || "")}"${dis}>同意执行</button>
      <button class="btn ghost sm" data-act="confirm-no" data-idx="${p.step_idx || 0}" data-tid="${esc(e.tid || "")}"${dis}>拒绝</button>
    </div></div></div>`;
}
function agentResultHtml(e) {
  const st = e.state || {};
  const sum = st.summary || {};
  const disp = agentDisplayState(st);
  const label = (AGENT_STATE_META[disp] || [])[0] || "已完成";
  const rows = [];
  if (sum.step_count != null) rows.push(["执行步骤", sum.step_count + " 个"]);
  if (sum.dedup_skipped) rows.push(["去重跳过", sum.dedup_skipped + " 个"]);
  if ((st.confirmed_steps || []).length) rows.push(["你确认的步骤", st.confirmed_steps.length + " 个"]);
  if (st.work_id) rows.push(["关联作品", st.work_id]);
  const hasJump = !!(st.work_id || (AGENT.ctx && (AGENT.ctx.product_id || AGENT.ctx.work_id)));
  return `<div class="agent-msg assistant"><div class="agent-result">
    <h4><span class="pill ${disp === "partial" ? "amber" : "green"}">${esc(label)}</span> 本次任务${disp === "partial" ? "部分步骤未成功，其余已完成" : "已完成，结果已保存"}</h4>
    ${rows.map(([k, v]) => `<div class="kv"><b>${esc(k)}</b><span>${esc(String(v))}</span></div>`).join("")}
    <div class="row">
      ${hasJump ? `<button class="btn primary sm" data-act="goto-result">去继续编辑 →</button>` : ""}
      <button class="btn ghost sm" data-act="copy-result">复制结果</button>
      <button class="btn ghost sm" data-act="regen">重新生成</button>
      <button class="btn ghost sm" data-act="new-run">开始新的需求</button>
    </div></div></div>`;
}
function agentErrorHtml(e) {
  const st = e.state || {};
  const sum = st.summary || {};
  const errMsg = sum.error || st.error || "未知错误";
  return `<div class="agent-msg assistant"><div class="agent-error">
    <h4>任务失败</h4>
    <p>${esc(sum.failed_step != null ? `第 ${sum.failed_step + 1} 个步骤失败：` : "")}${esc(errMsg)}</p>
    <div class="row">
      <button class="btn ghost sm" data-act="err-detail">查看错误详情</button>
      <button class="btn ghost sm" data-act="retry">重新发起</button>
    </div>
    <pre data-errbox style="display:none">${esc(JSON.stringify({ task_id: st.task_id, run_status: st.run_status, failed_step: sum.failed_step, error: errMsg, summary: sum }, null, 2))}</pre>
  </div></div>`;
}

/* ---------- 上下文 chip ---------- */
function renderAgentCtx() {
  const host = $("agent_ctx");
  if (!host) return;
  const c = AGENT.ctx;
  if (!c) { host.innerHTML = ""; return; }
  const parts = [`<span>来源：${esc(AGENT_SRC_LABEL[c.source] || c.source)}</span>`];
  if (c.work_id) parts.push(`<span>作品 <code>${esc(c.work_id)}</code></span>`);
  if (c.product_id) parts.push(`<span>产品档案 <code>${esc(c.product_id)}</code></span>`);
  if (c.shot_id != null) parts.push(`<span>镜头 <code>${esc(String(c.shot_id))}</code></span>`);
  parts.push(`<span style="color:var(--muted)">只传 ID，不复制整份资料</span>`);
  host.innerHTML = `<span class="agent-ctx-chip">📎 ${parts.join("")}<button data-act="ctx-clear" aria-label="清除上下文">×</button></span>`;
}

/* ---------- 计划侧栏 ---------- */
function renderAgentPlan(st) {
  const body = $("agent_plan_body");
  if (!body) return;
  if (!st || !AGENT.taskId) {
    body.innerHTML = `<div class="agent-plan-empty">暂无执行计划。<br>在左侧说一句话发送需求，AI 的执行步骤会实时显示在这里，关键步骤会先请你确认。</div>`;
    return;
  }
  const disp = agentDisplayState(st);
  const meta = AGENT_STATE_META[disp] || AGENT_STATE_META.idle;
  const prog = Math.max(0, Math.min(100, st.progress || 0));
  const pend = st.pending_step;
  const steps = st.steps || [];
  /* V3.2：执行中显示真实耗时（后端每步推进度，前端补时间维度） */
  const liveDisp = ["queued", "running", "cancelling"].includes(disp);
  const elapsedTxt = (liveDisp && AGENT.startedAt)
    ? agentFmtElapsed(Math.round((Date.now() - AGENT.startedAt) / 1000)) : "";
  const metaLine = elapsedTxt ? `${prog}% · ${elapsedTxt}` : `${prog}%`;
  let html = `<div class="agent-progress"><div class="meta"><span><span class="pill ${meta[1]}" style="margin-right:6px">${esc(meta[0])}</span></span><span>${esc(metaLine)}</span></div>
    <div class="progress"><i style="width:${prog}%"></i></div></div>`;
  /* V3.3：进行中的步骤（事件流推导）——侧栏不再只有干巴巴的已完成列表 */
  const curTools = agentRunningTools(st);
  if (curTools.length && !AGENT_TERMINAL.includes(agentNormStatus(st))) {
    html += `<div class="agent-step cur"><span class="n">⚙</span><div class="tx"><b>正在${esc(agentToolLabel(curTools[0]))}…</b><span>当前步骤进行中，完成会自动记入下方列表</span></div><span class="pill blue">进行中</span></div>`;
  }
  if (pend) {
    html += `<div class="agent-step cur"><span class="n">!</span><div class="tx"><b>${esc(agentToolLabel(pend.tool_id))}</b><span>步骤 ${(pend.step_idx || 0) + 1} · 等待你在左侧确认${pend.optional ? "（可选）" : ""}</span></div><span class="pill amber">待确认</span></div>`;
  }
  if (steps.length) {
    html += steps.map((s, i) => {
      const sm = AGENT_STEP_META[s.status] || AGENT_STEP_META.pending;
      return `<div class="agent-step${s.status === "running" || s.status === "waiting_user" ? " cur" : ""}">
        <span class="n">${i + 1}</span>
        <div class="tx"><b>${esc(agentToolLabel(s.tool_id))}</b>
        ${s.error ? `<span>✕ ${esc(String(s.error).slice(0, 120))}</span>` : (s.duration_ms != null ? `<span>用时 ${agentFmtDur(s.duration_ms)}</span>` : `<span>${esc(s.tool_id)}</span>`)}</div>
        <span class="pill ${sm[1]}">${sm[0]}</span></div>`;
    }).join("");
  } else if (!pend) {
    /* V3.2：首步编排期（无任何步骤落库）给出明确预期，不再像卡死 */
    html += `<div class="agent-plan-empty">正在解析需求并编排执行步骤…<br><span style="font-size:12px">首次编排通常需要 10–60 秒（取决于模型网关速度），出现步骤后这里会实时更新。</span></div>`;
  }
  if ((st.confirmed_steps || []).length) html += `<div class="agent-plan-empty" style="padding-top:8px">已确认 ${st.confirmed_steps.length} 个步骤</div>`;
  if (st.error && agentNormStatus(st) === "failed") html += `<div class="agent-plan-empty" style="color:#B03232">✕ ${esc(String(st.error).slice(0, 160))}</div>`;
  /* V3.6：生成结果查看区——成果写回了哪个作品、去哪看，一目了然 */
  const wid = st.work_id;
  if (wid && AGENT_TERMINAL.includes(agentNormStatus(st))) {
    html += `<div class="agent-results-box">
      <div class="t">📦 生成结果</div>
      <div class="d">本次全部成果已写入作品，点下方直接查看：</div>
      <button class="btn primary sm" data-act="open-result" data-wid="${esc(wid)}" data-view="v1" style="margin:8px 0 6px">打开作品台查看 →</button>
      <div class="agent-quick">
        <button data-act="open-result" data-wid="${esc(wid)}" data-view="v3">分镜 / 创意</button>
        <button data-act="open-result" data-wid="${esc(wid)}" data-view="v5">拍摄清单</button>
      </div></div>`;
  }
  body.innerHTML = html;
}

/* ---------- 轮询（2.5s，复用 pollAiTask 模式） ---------- */
function startAgentPoll() {
  stopAgentPoll();
  AGENT.timer = setInterval(agentPollTick, 2500);
  agentPollTick();
}
function stopAgentPoll() {
  if (AGENT.timer) { clearInterval(AGENT.timer); AGENT.timer = null; }
}
async function agentPollTick() {
  if (!AGENT.taskId) return;
  // S-03/O-07：失败退避期内跳过本拍（2.5s 定时器不停，只是不发请求），
  // 429/5xx/断网时不再持续 hammer；成功即清退避。
  if (AGENT.nextPollAt && Date.now() < AGENT.nextPollAt) return;
  let st = null;
  try {
    // O-01：增量轮询——只拉 seq > 上次已消费 seq 的事件（evSeq 为下个
    // 待消费 seq，故 after_seq = evSeq - 1；新任务 evSeq=0 时传 -1 全量）。
    const url = "/api/v1/agent/runs/" + encodeURIComponent(AGENT.taskId)
      + "?after_seq=" + (AGENT.evSeq - 1);
    st = V1(await ApiClient.get(url));
    AGENT.pollFails = 0; AGENT.nextPollAt = 0;
  } catch (e) {
    AGENT.pollFails++;
    // 指数退避 2.5s → 5s → 10s → 20s，上限 30s
    const waitMs = Math.min(2500 * Math.pow(2, AGENT.pollFails - 1), 30000);
    AGENT.nextPollAt = Date.now() + waitMs;
    if (AGENT.pollFails === 3) agentLog("sys", { text: "网络波动，"
      + Math.round(waitMs / 1000) + " 秒后重试轮询…", id: "poll-warn" });
    return;
  }
  if (!st) return;
  AGENT.lastState = st;
  const norm = agentNormStatus(st);
  let disp = agentDisplayState(st);
  /* V3.2：已请求取消但任务仍在收尾时，显示"取消中"而非"运行中"，
     避免用户以为取消没生效 */
  if (AGENT.cancelReq && !AGENT_TERMINAL.includes(norm)) disp = "cancelling";
  if (disp !== AGENT.state) {
    AGENT.state = disp;
    agentLog("sys", { text: disp === "cancelling"
      ? "正在取消…等待当前步骤结束（已 " + agentFmtElapsed(Math.round((Date.now() - (AGENT.startedAt || Date.now())) / 1000)) + "）"
      : "状态更新：" + (AGENT_STATE_META[disp] || [])[0],
      live: !AGENT_TERMINAL.includes(norm), id: "run-lifecycle" });
  }
  renderAgentPill(disp); setAgentButtons(disp);
  // O-01：增量事件本地合并（按 seq 去重排序）后再渲染；renderAgentEvents
  // 本身按 evSeq 幂等重放，agentRunningTools（侧栏“进行中步骤”）需要
  // 完整事件流推导，故传合并后的状态而非本拍增量。
  for (const ev of (st.events || [])) {
    if (!AGENT.allEvents.some((x) => x.seq === ev.seq)) AGENT.allEvents.push(ev);
  }
  AGENT.allEvents.sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const mst = { ...st, events: AGENT.allEvents };
  renderAgentEvents(mst); // V3.3：过程事件先于计划面板入聊天流
  renderAgentPlan(mst);
  if (norm === "waiting_user" && st.pending_step) agentMaybeConfirm(st);
  if (AGENT_TERMINAL.includes(norm)) { stopAgentPoll(); agentOnTerminal(st); }
}
function agentMaybeConfirm(st) {
  if (AGENT.renderedConfirmFor === AGENT.taskId) return;
  AGENT.renderedConfirmFor = AGENT.taskId;
  agentLog("confirm", { pending: st.pending_step, tid: AGENT.taskId });
}
function agentOnTerminal(st) {
  if (AGENT.renderedResultFor === AGENT.taskId) return;
  AGENT.renderedResultFor = AGENT.taskId;
  agentClearSnapshot(); // V3.5：终态后清刷新恢复快照
  const norm = agentNormStatus(st);
  AGENT.cancelReq = false; AGENT.startedAt = null; // V3.2：终态清标记
  // B-06：终态也回填 work_id（刷新恢复路径下 one-shot 响应已错过，
  // 这里补一次，后续追问才能走 /modify）
  if (st && st.work_id) {
    AGENT.ctx = { ...(AGENT.ctx || {}), work_id: st.work_id };
  }
  agentLog("sys", { text: "状态更新：" + (AGENT_STATE_META[agentDisplayState(st)] || [])[0], id: "run-lifecycle" });
  if (norm === "failed") agentLog("error", { state: st });
  else if (norm === "cancelled") agentLog("sys", { text: "任务已取消" });
  else agentLog("result", { state: st });
}

/* ---------- V3.5：刷新恢复（sessionStorage 快照，同标签页刷新不丢任务） ---------- */
function agentSaveSnapshot() {
  try {
    sessionStorage.setItem(AGENT.restoreKey, JSON.stringify({
      taskId: AGENT.taskId, convId: AGENT.convId,
      briefText: AGENT.lastBriefText || "", ctx: AGENT.ctx || null,
      startedAt: AGENT.startedAt || null,
    }));
  } catch (e) { /* 存储满/隐私模式：恢复功能降级，不影响主流程 */ }
}
function agentClearSnapshot() {
  try { sessionStorage.removeItem(AGENT.restoreKey); } catch (e) { /* 忽略 */ }
}
async function agentRestore() {
  if (AGENT.taskId || !loggedIn()) return;
  let snap = null;
  try { snap = JSON.parse(sessionStorage.getItem(AGENT.restoreKey) || "null"); } catch (e) { snap = null; }
  if (!snap || !snap.taskId) return;
  let st = null;
  try {
    st = V1(await ApiClient.get("/api/v1/agent/runs/" + encodeURIComponent(snap.taskId)));
  } catch (e) {
    const code = e && (e.status || e.code);
    if (code === 404 || code === 403 || code === 401) agentClearSnapshot(); // 任务过期/他组织：清快照
    return; // 网络波动：不清理，下次进视图再试
  }
  if (!st) return;
  // O-01：恢复首拍为全量事件， seeding 本地合并缓存，后续增量轮询才完整
  AGENT.allEvents = (st.events || []).slice();
  /* 恢复内存态：evSeq=0 让事件流全量重放；先补用户那句话让聊天流有上下文 */
  AGENT.taskId = snap.taskId;
  AGENT.convId = snap.convId || null;
  AGENT.lastBriefText = snap.briefText || "";
  AGENT.ctx = snap.ctx || null;
  AGENT.startedAt = snap.startedAt || Date.now();
  AGENT.evSeq = 0; AGENT.allEvents = []; AGENT.pollFails = 0;
  AGENT.renderedConfirmFor = null; AGENT.renderedResultFor = null;
  if (snap.briefText && AGENT.restoredFor !== snap.taskId) {
    AGENT.restoredFor = snap.taskId;
    agentLog("user", { text: snap.briefText });
    agentLog("sys", { text: "已恢复上次的任务记录（" + snap.taskId.slice(0, 8) + "…）", live: true, id: "run-lifecycle" });
  }
  const norm = agentNormStatus(st);
  AGENT.state = agentDisplayState(st); // 预置状态，避免恢复首拍打出多余的"状态更新"
  renderAgentCtx();
  if (AGENT_TERMINAL.includes(norm)) {
    AGENT.lastState = st;
    renderAgentPill(AGENT.state); setAgentButtons(AGENT.state);
    renderAgentEvents(st); renderAgentPlan(st);
    agentOnTerminal(st);
  } else {
    startAgentPoll(); // 首拍即同步全部过程事件并续轮询
  }
}

/* ---------- 发送 / 确认 / 取消 / 重试 ---------- */
/* V3.6：寒暄识别——纯打招呼/闲聊不建任务不进流水线（用户问"你好"不该触发 11 步） */
const AGENT_CHITCHAT_RE = /^(?:你好|您好|hi|hello|hey|嗨|哈喽|哈罗|在吗|在不在|在么|谢谢|多谢|感谢|辛苦了|麻烦了|再见|拜拜|晚安|早上好|上午好|中午好|下午好|晚上好|早安|午安|ok|okay|好|嗯|哦|嘿|哈呀?|哟){1,6}[的呀啊哈哇啦!！。~？?，,\s]*$/i;
const AGENT_CHITCHAT_ID_RE = /^(你是谁|你叫什么(名字)?|你是干啥(的)?|你是做什么的|你能做什么|你能干(什么|嘛|啥)|你会(什么|做啥)|怎么用(你)?|介绍下(你)?自己)[？?！!。~\s]*$/;
function agentIsChitchat(text) {
  const t = (text || "").trim();
  if (!t || t.length > 24) return false; // 长消息不拦
  if (AGENT_CHITCHAT_ID_RE.test(t)) return true; // 身份/能力问题优先判（"你能做什么"含"做"字）
  // 只要带创作/任务意图词，一律正常进流水线（"你好帮我做个视频"不是寒暄）
  if (/[视频文案分镜创意海报脚本口播带货营销推广拍摄写做生成发布产品帮我改剪辑修调抖音快手小红书方案]/.test(t)) return false;
  return AGENT_CHITCHAT_RE.test(t);
}
const AGENT_CHITCHAT_REPLY = "你好呀，我在的！把想做的内容说一句话就行，例如：「给章丘大葱做一条 60 秒抖音带货短视频，突出脆甜」。我会自己安排查事实、出创意、排分镜，关键步骤会先请你确认。";
const AGENT_CHITCHAT_SUGGESTS = ["给章丘大葱做一条 60 秒抖音带货短视频", "检查当前作品的风险项并给出修改建议", "生成 3 个创意方向"];
/* B-06：/modify 返回的修改摘要拼成一句人话（changed 可能是字符串/
   数组/对象，三种都兜底；不依赖具体字段名以外的结构）。 */
function agentModifySummaryText(m) {
  const changed = m.changed;
  let c = "";
  if (typeof changed === "string") c = changed;
  else if (Array.isArray(changed)) c = changed.join("；");
  else if (changed && typeof changed === "object") c = Object.values(changed).join("；");
  const shots = (m.impacted_shots || []).map((n) => "镜" + n).join("、");
  let t = "已按你的要求局部修改完成";
  if (c) t += "：" + c;
  if (shots) t += "（涉及" + shots + "）";
  t += "，作品已更新到 v" + (m.work_version || "?") + "。";
  return t;
}
async function agentSend() {
  const ta = $("agent_input");
  const text = (ta.value || "").trim();
  if (AGENT.state === "sending" || (AGENT.timer && AGENT.state !== "idle")) { toast("当前任务还在执行，稍后再发"); return; }
  if (!loggedIn()) { toast("请先登录"); go("v-login"); return; }
  if (!S.org) { toast("尚未加入组织，请先完成注册"); return; }
  if (!text) { toast("先说一句话，描述你想让 AI 做什么"); ta.focus(); return; }
  /* V3.6：寒暄直答——不建任务、不进流水线、不占配额 */
  if (AGENT.state === "idle" && agentIsChitchat(text)) {
    agentLog("user", { text });
    agentLog("ai", { text: AGENT_CHITCHAT_REPLY, suggests: AGENT_CHITCHAT_SUGGESTS });
    ta.value = ""; ta.focus();
    return;
  }
  AGENT.state = "sending";
  AGENT.lastBriefText = text;
  agentLog("user", { text });
  renderAgentPill("sending"); setAgentButtons("sending");
  $("btn_agent_send").disabled = true;
  const ctx = AGENT.ctx || {};
  const brief = { text, source: ctx.source || "chat",
    product_id: ctx.product_id || null, work_id: ctx.work_id || null,
    shot_id: ctx.shot_id == null ? null : ctx.shot_id };
  try {
    // B-06：终态后追问——同一会话已有作品时走 /modify 局部修改，
    // 不再新建 11 步 one-shot（改第 3 镜之类的追问只改局部）。
    const lastNorm = AGENT.lastState ? agentNormStatus(AGENT.lastState) : null;
    const modifyWorkId = (AGENT.ctx && AGENT.ctx.work_id)
      || (AGENT.lastState && AGENT.lastState.work_id) || null;
    if (AGENT.convId && modifyWorkId
        && (lastNorm === "completed" || lastNorm === "partial")) {
      const m = V1(await ApiClient.post("/api/v1/agent/modify", {
        org_id: S.org.id, work_id: modifyWorkId,
        conversation_id: AGENT.convId, user_text: text.slice(0, 2000),
      }, { timeoutMs: ApiClient.AI_TIMEOUT_MS })); // 同步接口，走 AI 超时档
      ta.value = "";
      if (m && m.status === "waiting_user" && m.task_id) {
        AGENT.taskId = m.task_id;
        AGENT.evSeq = 0; AGENT.allEvents = []; // O-01：新 task 的事件流从头开始
        AGENT.renderedConfirmFor = null; AGENT.renderedResultFor = null;
        AGENT.pollFails = 0; AGENT.state = "waiting_user";
        agentLog("sys", { text: "这次修改需要你确认，确认卡片已生成",
                         live: true, id: "run-lifecycle" });
        renderAgentPill("waiting_user"); setAgentButtons("waiting_user");
        startAgentPoll();
      } else if (m && m.status === "completed") {
        AGENT.state = "completed";
        agentLog("ai", { text: agentModifySummaryText(m) });
        renderAgentPill("completed"); setAgentButtons("completed");
      } else {
        AGENT.state = "idle";
        agentLog("sys", { text: "修改未完成："
          + ((m && (m.error || m.status)) || "未知状态") });
        renderAgentPill("idle"); setAgentButtons("idle");
      }
      return;
    }
    // 一句话生成：调 /one-shot（建 run + 入队异步执行），不再只建不执行
    const body = { org_id: S.org.id, brief_text: text.slice(0, 2000) };
    if (brief.work_id) body.work_id = brief.work_id;
    // Phase 5 多轮串联：同一会话内后续消息复用 conversation（brief 增量更新）
    if (AGENT.convId) body.conversation_id = AGENT.convId;
    const r = V1(await ApiClient.post("/api/v1/agent/one-shot", body,
      { idempotencyKey: "auto" })); // B-05：双击/重试不再建两个 run
    AGENT.taskId = r.task_id;
    AGENT.convId = r.conversation_id || null;
    // B-06：新建作品 id 写回 AGENT.ctx，后续追问才能走 /modify
    if (r.work_id) {
      AGENT.ctx = { ...(AGENT.ctx || {}), work_id: r.work_id };
      renderAgentCtx();
    }
    AGENT.renderedConfirmFor = null; AGENT.renderedResultFor = null; AGENT.pollFails = 0;
    AGENT.cancelReq = false; AGENT.startedAt = Date.now(); // V3.2
    AGENT.evSeq = 0; AGENT.allEvents = []; // V3.3：新任务事件流从头重放
    agentSaveSnapshot(); // V3.5：刷新可恢复
    AGENT.state = agentNormStatus(r);
    agentLog("sys", { text: "任务已创建（" + AGENT.taskId.slice(0, 8) + "…），正在排队执行，完成后这里会实时更新", live: true, id: "run-lifecycle" });
    renderAgentPill(AGENT.state); setAgentButtons(AGENT.state); renderAgentPlan(AGENT.lastState);
    ta.value = "";
    startAgentPoll();
  } catch (e) {
    AGENT.state = "idle";
    renderAgentPill("idle"); setAgentButtons("idle");
    agentLog("sys", { text: "发送失败：" + (e.message || e) });
    toast("创建任务失败：" + (e.message || e));
  } finally {
    $("btn_agent_send").disabled = false;
  }
}
async function agentConfirm(stepIdx, approved, btn) {
  if (btn) btn.disabled = true;
  try {
    await ApiClient.post("/api/v1/agent/runs/" + encodeURIComponent(AGENT.taskId) + "/confirm",
      { step_idx: stepIdx, approved });
    if (approved) {
      agentLog("sys", { text: "已确认，继续执行…", live: true, id: "run-lifecycle" });
      AGENT.state = "running"; renderAgentPill("running"); setAgentButtons("running");
      if (!AGENT.timer) startAgentPoll(); else agentPollTick();
    } else {
      agentLog("sys", { text: "已拒绝该步骤", id: "run-lifecycle" });
      if (!AGENT.timer) startAgentPoll(); else agentPollTick();
    }
  } catch (e) {
    toast("确认失败：" + (e.message || e));
    if (btn) btn.disabled = false;
  }
}
async function agentPause() {
  if (!AGENT.taskId) return;
  try {
    await ApiClient.post("/api/v1/agent/runs/" + encodeURIComponent(AGENT.taskId) + "/pause", {});
    toast("已请求暂停，当前步骤完成后挂起");
    agentLog("sys", { text: "已请求暂停…", live: true, id: "run-lifecycle" });
    agentPollTick();
  } catch (e) { toast("暂停失败：" + (e.message || e)); }
}
async function agentResume() {
  if (!AGENT.taskId) return;
  try {
    await ApiClient.post("/api/v1/agent/runs/" + encodeURIComponent(AGENT.taskId) + "/resume", {});
    toast("已继续执行");
    agentLog("sys", { text: "已继续，恢复执行…", live: true, id: "run-lifecycle" });
    AGENT.state = "running"; renderAgentPill("running"); setAgentButtons("running");
    if (!AGENT.timer) startAgentPoll(); else agentPollTick();
  } catch (e) { toast("继续失败：" + (e.message || e)); }
}
async function agentCancel() {
  if (!AGENT.taskId) return;
  if (AGENT.cancelReq) return; // V3.2：已请求过取消，防重复点
  try {
    /* V3.2：先给即时反馈再发请求——后端是协作式取消（当前模型调用结束后
       才能真正停止，最长约 1–2 分钟），必须让用户知道"点了就生效，
       剩下的是等当前步骤收尾"，否则像卡死。 */
    AGENT.cancelReq = true;
    renderAgentPill("cancelling"); setAgentButtons("cancelling");
    agentLog("sys", { text: "已发出取消请求，当前模型调用结束后立即停止（最长约 1–2 分钟）", live: true, id: "run-lifecycle" });
    await ApiClient.post("/api/v1/agent/runs/" + encodeURIComponent(AGENT.taskId) + "/cancel", {});
    toast("取消请求已提交");
    if (!AGENT.timer) startAgentPoll(); else agentPollTick();
  } catch (e) {
    AGENT.cancelReq = false;
    renderAgentPill(AGENT.state); setAgentButtons(AGENT.state);
    toast("取消失败：" + (e.message || e));
  }
}
function agentRetry() {
  const t = AGENT.lastBriefText;
  if (!t) { toast("没有可重试的内容"); return; }
  stopAgentPoll();
  AGENT.taskId = null; AGENT.convId = null; AGENT.state = "idle";
  AGENT.lastState = null; AGENT.renderedConfirmFor = null; AGENT.renderedResultFor = null;
  agentClearSnapshot(); // V3.5：重试是新任务，清旧快照
  AGENT.cancelReq = false; AGENT.startedAt = null; // V3.2
  AGENT.evSeq = 0; AGENT.allEvents = []; // V3.3
  $("agent_input").value = t;
  renderAgentPill("idle"); setAgentButtons("idle");
  agentSend();
}
function agentCopyResult(btn) {
  const card = btn ? btn.closest(".agent-result") : null;
  if (!card) { toast("没有可复制的内容"); return; }
  // 取卡片纯文本（去掉按钮行）
  const clone = card.cloneNode(true);
  clone.querySelectorAll(".row").forEach((r) => r.remove());
  const text = (clone.innerText || "").trim();
  if (!text) { toast("没有可复制的内容"); return; }
  const done = () => toast("结果已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); }
  catch (e) { toast("复制失败，请手动选择复制"); }
  document.body.removeChild(ta);
}
async function agentGotoResult() {
  const st = AGENT.lastState || {};
  const ctx = AGENT.ctx || {};
  const workId = st.work_id || ctx.work_id;
  if (ctx.product_id) S.selectedProductId = ctx.product_id;
  const view = AGENT_SRC_VIEW[ctx.source] || "v1";
  if (workId) {
    try { await openWork(workId, view); }
    catch (e) { toast("打开作品失败：" + (e.message || e)); go(view); }
  } else go(view);
}
/* 从会话消息恢复（刷新恢复用：后端 conversation 已持久化） */
async function agentRebuildFromMessages() {
  if (!AGENT.convId || AGENT.log.length) return;
  try {
    const r = V1(await ApiClient.get("/api/v1/agent/conversations/" + encodeURIComponent(AGENT.convId) + "/messages?limit=50"));
    const items = (r && r.items) || [];
    for (const m of items) {
      const c = m.content || {};
      if (m.role === "user" && c.type === "brief" && c.brief && c.brief.text) agentLog("user", { text: c.brief.text });
      else if (m.role === "assistant" && c.type === "run_cancelled") agentLog("sys", { text: "任务已取消" });
    }
  } catch (e) { /* 恢复失败不阻断，轮询仍会拿到最新状态 */ }
}

/* ---------- 入口 ---------- */
function openAgent(src) {
  if (!loggedIn()) { toast("请先登录"); go("v-login"); return; }
  const workId = (S.activeWork && S.activeWork.id) || null;
  const productId = S.selectedProductId || S.genProductId || null;
  let shotId = null;
  if (src === "v3_storyboard") {
    const sh = ((S.data && S.data.storyboard && S.data.storyboard.shots) || [])[0];
    if (sh && sh.shot_number != null) shotId = sh.shot_number;
  }
  AGENT.ctx = { source: src, product_id: productId, work_id: workId, shot_id: shotId };
  const ta = $("agent_input");
  if (ta && !ta.value.trim()) ta.value = AGENT_SRC_HINT[src] || "";
  renderAgentCtx();
  go("agent");
  setTimeout(() => { const t = $("agent_input"); if (t) t.focus(); }, 350);
}
/* 今日配图额度：与 POST /poster/image 同一口径（后端 services/poster_quota.py）。
   只读展示，不扣额度；读不到就隐藏，不挡使用。 */
async function loadAgentQuota() {
  const pill = $("agent_quota_pill");
  if (!pill) return;
  if (!S.org || !S.org.id) { pill.style.display = "none"; return; }
  try {
    const q = V1(await ApiClient.get(
      "/api/v1/agent/quota?org_id=" + encodeURIComponent(S.org.id)));
    if (!q) { pill.style.display = "none"; return; }
    const left = typeof q.remaining === "number" ? q.remaining : null;
    // M4：未知额度不伪装成"不限"；时间口径统一为北京时间 8 点恢复。
    if (q.unknown) {
      pill.className = "pill gray";
      pill.textContent = "配图额度未知";
    } else if (q.enforced === false || left === null) {
      pill.className = "pill gray";
      pill.textContent = "配图不限次";
    } else if (left <= 0) {
      pill.className = "pill red";
      pill.textContent = "今日配图已用完 · 明日 8 点（北京时间）恢复";
    } else {
      pill.className = "pill " + (left <= 2 ? "amber" : "green");
      pill.textContent = `今日配图剩余 ${left} 次`;
    }
    pill.title = q.reason || "";
    pill.style.display = "";
  } catch (e) {
    // 额度查询失败不再静默隐藏：显示“未知”，不伪装成“不限”也不显示错误数字。
    pill.className = "pill gray";
    pill.textContent = "配图额度未知";
    pill.title = "";
    pill.style.display = "";
  }
}
function enterAgentView() {
  renderAgentCtx();
  loadAgentQuota();
  renderAgentPill(AGENT.state); setAgentButtons(AGENT.state);
  renderAgentLog(); renderAgentPlan(AGENT.lastState);
  agentRestore(); // V3.5：刷新后自动找回上次任务并重放过程记录
  const narrow = window.matchMedia && window.matchMedia("(max-width:960px)").matches;
  const plan = $("agent_plan");
  if (plan) { plan.classList.toggle("open", !narrow); $("btn_agent_plan_toggle").setAttribute("aria-expanded", String(!narrow)); $("btn_agent_plan_toggle").textContent = narrow ? "展开" : "收起"; }
  if (AGENT.pendingSend) { const t = AGENT.pendingSend; AGENT.pendingSend = ""; $("agent_input").value = t; agentSend(); return; }
  if (AGENT.taskId && AGENT.lastState && !AGENT_TERMINAL.includes(agentNormStatus(AGENT.lastState)) && !AGENT.timer) startAgentPoll();
  if (AGENT.convId && !AGENT.log.length) agentRebuildFromMessages();
}
function initAgentBindings() {
  $("btn_agent_send").addEventListener("click", agentSend);
  $("agent_input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); agentSend(); }
  });
  $("btn_agent_cancel").addEventListener("click", agentCancel);
  $("btn_agent_pause").addEventListener("click", agentPause);
  $("btn_agent_resume").addEventListener("click", agentResume);
  $("btn_agent_retry").addEventListener("click", agentRetry);
  $("btn_agent_plan_toggle").addEventListener("click", () => {
    const plan = $("agent_plan");
    const open = plan.classList.toggle("open");
    const t = $("btn_agent_plan_toggle");
    t.setAttribute("aria-expanded", String(open));
    t.textContent = open ? "收起" : "展开";
  });
  $("btn_v0_agent_go").addEventListener("click", () => {
    if (!loggedIn()) { toast("请先登录"); go("v-login"); return; }
    const text = ($("v0_agent_brief").value || "").trim();
    const workId = (S.activeWork && S.activeWork.id) || null;
    AGENT.ctx = { source: "v0_home", product_id: null, work_id: workId, shot_id: null };
    renderAgentCtx();
    go("agent");
    if (text) AGENT.pendingSend = text;
    else setTimeout(() => { const t = $("agent_input"); if (t) t.focus(); }, 350);
  });
  $("v0_agent_brief").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("btn_v0_agent_go").click(); }
  });
  document.querySelectorAll("[data-agent-src]").forEach((b) =>
    b.addEventListener("click", () => openAgent(b.dataset.agentSrc)));
  $("agent_ctx").addEventListener("click", (e) => {
    if (e.target.closest('[data-act="ctx-clear"]')) { AGENT.ctx = null; renderAgentCtx(); }
  });
  /* V3.6：生成结果跳转（事件委托，面板随轮询重渲染不影响监听） */
  $("agent_plan").addEventListener("click", (e) => {
    const el = e.target.closest('[data-act="open-result"]');
    if (!el || !el.dataset.wid) return;
    openWork(el.dataset.wid, el.dataset.view)
      .catch((er) => toast("打开作品失败：" + (er.message || er)));
  });
  $("agent_msgs").addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "suggest") { $("agent_input").value = el.dataset.text || ""; $("agent_input").focus(); }
    else if (act === "confirm-yes") { if (el.dataset.tid && el.dataset.tid !== AGENT.taskId) { toast("该确认已过期"); } else agentConfirm(parseInt(el.dataset.idx || "0", 10), true, el); }
    else if (act === "confirm-no") { if (el.dataset.tid && el.dataset.tid !== AGENT.taskId) { toast("该确认已过期"); } else agentConfirm(parseInt(el.dataset.idx || "0", 10), false, el); }
    else if (act === "err-detail") {
      const pre = el.closest(".agent-error").querySelector("[data-errbox]");
      const show = pre.style.display === "none";
      pre.style.display = show ? "" : "none";
      el.textContent = show ? "收起错误详情" : "查看错误详情";
    }
    else if (act === "retry") agentRetry();
    else if (act === "regen") agentRetry();
    else if (act === "copy-result") agentCopyResult(el);
    else if (act === "goto-result") agentGotoResult();
    else if (act === "new-run") { $("agent_input").value = ""; $("agent_input").focus(); }
  });
}

/* V4B-TEST-MARKER: 只读测试钩子（供 Playwright 验证读取内部状态，不写生产逻辑） */
window.__agentProbe = function () {
  return { taskId: AGENT.taskId, state: AGENT.state, polling: !!AGENT.timer,
           fails: AGENT.pollFails, logLen: AGENT.log.length };
};
/* ---------- 认证 ---------- */
function setAuthMode(mode) {
  S.authMode = mode;
  $("tab_login").classList.toggle("active", mode === "login");
  $("tab_register").classList.toggle("active", mode === "register");
  $("row_display_name").style.display = mode === "register" ? "" : "none";
  $("auth_title").textContent = mode === "login" ? "欢迎回来" : "创建你的工作室";
  $("btn_do_auth").textContent = mode === "login" ? "登录" : "注册并登录";
  errText("login_err", "");
}

async function doAuth() {
  const email = $("login_email").value.trim();
  const password = $("login_password").value;
  if (!email || !password) { errText("login_err", "请填写邮箱和密码"); return; }
  if (S.authMode === "register" && password.length < 6) { errText("login_err", "密码至少 6 位"); return; }
  const btn = $("btn_do_auth");
  btn.disabled = true;
  try {
    if (S.authMode === "register") {
      const name = $("login_name").value.trim();
      await AuthClient.register(email, password, name);
      // 后端 register 只返回用户信息不下发 token，注册完再登一次
      await AuthClient.login(email, password);
    } else {
      await AuthClient.login(email, password);
    }
    S.user = AuthClient.getUser();
    await ensureOrg();
    renderUser();
    toast("登录成功");
    go("v1");
  } catch (e) {
    errText("login_err", e.message || "登录失败");
  } finally {
    btn.disabled = false;
  }
}

async function ensureOrg() {
  const orgs = V1(await ApiClient.get("/api/v1/orgs")) || [];
  if (orgs.length) { setActiveOrg(orgs[0]); return; }
  const name = (S.user && (S.user.display_name || S.user.email.split("@")[0]) || "我") + "的工作室";
  setActiveOrg(V1(await ApiClient.post("/api/v1/orgs", { name })));
}

/* 组织是 ApiClient 的隐式参数（X-Org-Id / ?org_id=），而 ApiClient 在独立 IIFE 里
   看不到本文件的 S —— 必须显式注册，否则预算与事实 grounding 静默失效。 */
function setActiveOrg(org) {
  S.org = org || null;
  try { ApiClient.setOrg(S.org ? S.org.id : ""); } catch (e) { /* 忽略 */ }
}

async function doLogout() {
  await AuthClient.logout();
  S.user = null; S.works = []; S.activeWork = null; S.data = blankData(); S.facts = [];
  setActiveOrg(null); // 必须清空，否则换账号后仍带上一账号的 org_id
  agentClearSnapshot(); // V3.5：登出清恢复快照，防止换账号恢复出上一账号的任务视图
  renderUser();
  // 登出后回到公开展示页（三层架构）
  location.href = "/";
}

function renderUser() {
  const u = S.user;
  const name = u ? (u.display_name || u.email) : "未登录";
  $("user_name").textContent = name;
  $("user_avatar").textContent = name.slice(0, 1).toUpperCase();
  $("user_sub").textContent = u ? (S.org ? S.org.name : "") : "点击登录";
  $("top_user").style.display = u ? "" : "none";
  $("top_user").textContent = name;
  $("btn_logout").style.display = u ? "" : "none";
}

/* ---------- 作品 ---------- */
async function loadWorks() {
  const list = V1(await ApiClient.get("/api/v1/works?org_id=" + encodeURIComponent(S.org.id))) || [];
  S.works = list;
  if (!S.activeWork || !list.some((w) => w.id === S.activeWork.id)) {
    S.activeWork = list[0] || null;
  } else {
    const fresh = list.find((w) => w.id === S.activeWork.id);
    if (fresh) S.activeWork = fresh;
  }
  S.data = blankData();
  if (S.activeWork && S.activeWork.data) Object.assign(S.data, JSON.parse(JSON.stringify(S.activeWork.data)));
  // V3.0 B-4/B-5：恢复生成时的产品引用记录
  S.productGenRef = (S.data && S.data.product_ref) || null;
}

async function createWork(name) {
  const title = (name || "").trim() || ("作品 " + (S.works.length + 1));
  const w = V1(await ApiClient.post("/api/v1/works", { org_id: S.org.id, title, data: blankData() }));
  S.activeWork = w;
  S.data = blankData();
  await loadWorks();
  S.activeWork = S.works.find((x) => x.id === w.id) || w;
  S.data = blankData();
  S.productGenRef = null; // V3.0 B-4/B-5：新作品无产品引用
  Object.assign(S.data, JSON.parse(JSON.stringify(S.activeWork.data || {})));
  return S.activeWork;
}

async function saveWork() {
  if (!S.activeWork) return;
  try {
    // V3.0 B-4/B-5：把生成时记录的产品引用版本随作品保存（后端只做标记）
    if (S.productGenRef !== undefined) S.data.product_ref = S.productGenRef;
    const w = V1(await ApiClient.patch("/api/v1/works/" + S.activeWork.id,
      { data: S.data, version: S.activeWork.version }));
    S.activeWork = w;
    const i = S.works.findIndex((x) => x.id === w.id);
    if (i >= 0) S.works[i] = w;
  } catch (e) {
    if (e && e.status === 409) {
      // V2.1：409 版本冲突不再静默丢数据——让用户二选一
      const keep = await modalConfirm({
        title: "作品版本冲突",
        body: "作品在别处被修改过，本地还有未保存的内容。\n\n点「保留我的修改」= 强制保存（覆盖别处的修改）\n点「放弃我的修改」= 加载最新版本",
        okText: "保留我的修改", cancelText: "放弃我的修改",
      });
      if (keep) {
        try {
          const fresh = V1(await ApiClient.get("/api/v1/works/" + S.activeWork.id));
          S.activeWork.version = fresh.version;
          const w = V1(await ApiClient.patch("/api/v1/works/" + S.activeWork.id,
            { data: S.data, version: S.activeWork.version }));
          S.activeWork = w;
          const i = S.works.findIndex((x) => x.id === w.id);
          if (i >= 0) S.works[i] = w;
          toast("已强制保存，覆盖了别处的修改");
        } catch (e2) {
          toast("强制保存失败：" + (e2.message || "网络错误") + "，你的修改还在本地，请稍后重试");
        }
      } else {
        await loadWorks();
        renderWorkbench();
        toast("已放弃本地修改，加载了最新版本");
      }
    } else {
      toast("保存失败：" + (e.message || "网络错误"));
    }
  }
}

async function openWork(id, view) {
  const w = V1(await ApiClient.get("/api/v1/works/" + id));
  S.activeWork = w;
  S.data = blankData();
  Object.assign(S.data, JSON.parse(JSON.stringify(w.data || {})));
  S.ideaSel = -1;
  go(view || nextViewFor(S.data));
}

async function deleteWork(id) {
  const w = S.works.find((x) => x.id === id);
  if (!w) return;
  if (!(await modalConfirm({ title: "删除作品", body: `确定删除作品「${w.title}」吗？\n此操作不可恢复。`, okText: "删除", danger: true }))) return;
  await ApiClient.del("/api/v1/works/" + id);
  if (S.activeWork && S.activeWork.id === id) { S.activeWork = null; S.data = blankData(); }
  await loadWorks();
  renderWorkbench();
  toast("已删除");
}

function workProgress(d) {
  const shots = (d.storyboard && d.storyboard.shots) || [];
  const done = Object.values(d.shoot_state || {}).filter((s) => s.status === "done").length;
  let pct = 0, stage = "产品资料";
  if (d.product && d.product.product_name) { pct = 20; stage = "创意策划"; }
  if ((d.ideas || []).length) { pct = 40; stage = "分镜编辑"; }
  if (shots.length) { pct = 60; stage = "拍摄执行"; }
  if (d.feasibility) { pct = Math.max(pct, 70); }
  if (shots.length && done === shots.length && done > 0) { pct = 90; stage = "交付"; }
  if ((d.risk || []).length) { pct = Math.max(pct, 85); }
  // P-6：百分比是阶段映射的假进度，呈现改「阶段名 + 步点」；这里给步序号（共 5 步）
  const STAGE_IDX = { "产品资料": 1, "创意策划": 2, "分镜编辑": 3, "拍摄执行": 4, "交付": 5 };
  return { pct, stage, step: STAGE_IDX[stage] || 1, steps: 5, shots: shots.length, done };
}
function stepDots(p) {
  return `<span class="sdots" role="img" aria-label="第 ${p.step} 步，共 ${p.steps} 步">${"●".repeat(p.step)}${"○".repeat(p.steps - p.step)}</span>`;
}

function nextViewFor(d) {
  if (!d.product) return "v2";
  if (!(d.ideas || []).length) return "v3";
  if (!((d.storyboard || {}).shots || []).length) return "v3";
  return "v5";
}

/* ---------- v0 项目总览（I-8：首页由营销复刻改仪表盘，与 v1 工作台分工） ---------- */
async function renderOverview() {
  const host = $("v0_overview");
  if (!host) return;
  if (!loggedIn() || !S.org) { host.innerHTML = ""; return; }
  try { await loadWorks(); } catch (e) { /* 总览加载失败不阻断首页 */ }
  const all = (S.works || []).slice();
  const active = all.filter((w) => (w.status || "draft") !== "archived");
  // 待办统计：镜头待验收（shoot_state=shot）+ 风险待确认（risk.confirmed !== true）
  let pendingShots = 0, pendingRisks = 0;
  active.forEach((w) => {
    const d = w.data || {};
    Object.values(d.shoot_state || {}).forEach((s) => { if (s && s.status === "shot") pendingShots++; });
    (Array.isArray(d.risk) ? d.risk : []).forEach((f) => { if (f && f.confirmed !== true) pendingRisks++; });
  });
  // 事实待确认（组织级待审核数，取不到时不展示该项，避免假数字）
  let pendingFacts = -1;
  try {
    const facts = V1(await ApiClient.get("/api/v1/facts?org_id=" + encodeURIComponent(S.org.id))) || [];
    pendingFacts = facts.filter((f) => f.status === "pending_review").length;
  } catch (e) { /* 静默：不展示 */ }
  const byRecent = (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
  const last = active.slice().sort(byRecent)[0];
  const lp = last ? workProgress(last.data || {}) : null;
  const lastName = last ? (((last.data || {}).product || {}).product_name || last.title || "未命名作品") : "";
  // I-4：待办落点带作品——分别找「含待验收镜头最多」「含待确认风险最多」的作品，点卡直接落到该作品的对应视图
  const argmaxWork = (score) => active.slice().sort((a, b) => score(b) - score(a))[0];
  const shotScore = (w) => Object.values((w.data || {}).shoot_state || {}).filter((x) => x && x.status === "shot").length;
  const riskScore = (w) => (Array.isArray((w.data || {}).risk) ? (w.data || {}).risk : []).filter((f) => f && f.confirmed !== true).length;
  const shotWork = pendingShots ? argmaxWork(shotScore) : null;
  const riskWork = pendingRisks ? argmaxWork(riskScore) : null;
  const todo = (n, label, view, color, work) =>
    `<button class="ov-todo${n === 0 ? " quiet" : ""}" data-ov-go="${view}"${work ? ` data-ov-work="${esc(work.id)}"` : ""}><b style="color:${n === 0 ? "var(--muted)" : color}">${n}</b><span>${label}</span></button>`;
  host.innerHTML = `<div class="ov-wrap">
    ${last ? `<div class="card ov-last">
        <div class="ov-last-tx"><span class="pill amber">继续上次作品</span><h3>${esc(lastName)}</h3>
        <div class="meta"><span>当前阶段：${esc(lp.stage)}</span>${stepDots(lp)}</div></div>
        <button class="btn primary" data-ov-open="${esc(last.id)}">继续创作 →</button>
      </div>`
    : `<div class="card ov-last">${emptyState("还没有作品", "从一件山东好物开始。", false, { label: "新建作品", fn: () => { go("v1"); newWorkFlow(); } })}</div>`}
    <div class="ov-todos">
      ${todo(pendingShots, "个镜头待验收", "v5", "var(--primary)", shotWork)}
      ${todo(pendingRisks, "条风险待确认", "v3", "var(--red)", riskWork)}
      ${pendingFacts >= 0 ? todo(pendingFacts, "条事实待确认", "v2", "var(--amber-deep)", last) : ""}
    </div>
    ${all.length ? `<div class="card ov-recent"><h3 class="ov-h">最近动态</h3>${all.slice().sort(byRecent).slice(0, 4).map((w) => {
      const d = w.data || {}; const p = workProgress(d);
      const nm = ((d.product || {}).product_name) || w.title || "未命名作品";
      return `<button class="ov-row" data-ov-open="${esc(w.id)}"><span class="ov-row-name">${esc(nm)}</span><span class="pill gray">${esc(p.stage)}</span><span class="ov-row-date">${esc((w.updated_at || "").slice(0, 16).replace("T", " "))}</span></button>`;
    }).join("")}</div>` : ""}
  </div>`;
  host.querySelectorAll("[data-ov-open]").forEach((b) => b.addEventListener("click", () => openWork(b.dataset.ovOpen)));
  host.querySelectorAll("[data-ov-go]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.ovWork) openWork(b.dataset.ovWork, b.dataset.ovGo);
    else go(b.dataset.ovGo);
  }));
}

/* ---------- v1 工作台 ---------- */
const COVERS = [
  "linear-gradient(135deg,#C62828,#C29C5F)", "linear-gradient(135deg,#0F5132,#2E7D6B)",
  "linear-gradient(135deg,#2E7D6B,#7FB5A3)", "linear-gradient(135deg,#8A5A2B,#C29C5F)",
];
async function renderWorkbench() {
  if (!S.org) return;
  try {
    await loadWorks();
  } catch (e) {
    $("greet_text").textContent = "加载失败：" + (e.message || "网络错误");
    return;
  }
  // 问候语：真实数字（不用全平台用量口径，避免"像写死的数字"）
  const activeCount = S.works.filter((w) => (w.status || "draft") !== "archived").length;
  const doneCount = S.works.filter((w) => w.status === "archived").length;
  const hour = new Date().getHours();
  const hi = hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
  $("greet_title").textContent = `${hi}。`;
  $("greet_text").innerHTML = S.works.length === 0
    ? `还没有作品，点右上角「新建作品」，从一件山东好物开始。`
    : `你有 <b>${activeCount}</b> 个作品正在推进` + (doneCount ? `，<b>${doneCount}</b> 个已交付` : ``) + `。`;

  // I-2：问候行右侧「继续上次作品」主按钮（取最近更新的进行中作品）
  const cont = $("greet_continue");
  if (cont) {
    const lastW = S.works.filter((w) => (w.status || "draft") !== "archived")
      .slice().sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))[0];
    if (lastW) {
      const nm = ((lastW.data || {}).product || {}).product_name || lastW.title || "未命名作品";
      cont.style.display = "";
      cont.textContent = `继续《${nm}》 →`;
      cont.onclick = () => openWork(lastW.id);
    } else cont.style.display = "none";
  }

  // 作品卡片
  const grid = $("work_grid");
  let items = S.works.slice();
  if (S.workFilter === "active") items = items.filter((w) => (w.status || "draft") !== "archived");
  if (S.workFilter === "done") items = items.filter((w) => (w.status || "") === "archived");
  if (!items.length) {
    grid.className = "work-grid stagger";
    grid.innerHTML = '<div style="grid-column:1/-1">' + emptyState("暂无作品", "从一件山东好物开始。", false, { label: "新建作品", fn: () => newWorkFlow() }) + '</div>';
  } else {
    // V3.7：按状态归拢分组——进行中 / 已归档 各自成组，组内按更新时间倒序
    const byRecent = (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
    const workCardHtml = (w, i) => {
      const d = w.data || {};
      const p = workProgress(d);
      const name = (d.product && d.product.product_name) || w.title;
      const done = w.status === "archived";
      const origin = ((d.product || {}).origin || "").trim();
      // I-9：封面优先真实图（海报 AI 图 → 素材首图懒加载），无图回退 COVERS 渐变
      const aiImg = ((d.poster || {}).ai_image || "").trim();
      return `<div class="card work" data-id="${esc(w.id)}">
        <div class="cover"${aiImg ? "" : ` data-cover-need="${esc(w.id)}"`} style="background:${COVERS[i % COVERS.length]}">${aiImg ? `<img class="cover-img" alt="" src="${esc(aiImg)}">` : ""}${done ? '<span class="stamp-badge">已归档</span>' : '<span class="pill amber">进行中</span>'}</div>
        <div class="body"><h4>${esc(name)}</h4><div style="display:flex;align-items:center;gap:8px"><div class="date">${esc((w.updated_at || "").slice(0, 16).replace("T", " "))}</div>${origin ? `<span class="region-tag" style="margin-left:auto">${esc(origin)}</span>` : ""}</div>
        <div class="meta"><span>${esc(p.stage)}</span>${stepDots(p)}</div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn ghost sm" data-act="open" style="flex:1;justify-content:center">继续创作</button>
          ${done ? "" : `<button class="btn ghost sm" data-act="arch" title="归档">归档</button>`}
          <button class="btn danger sm" data-act="del" title="删除作品">删除</button>
        </div></div></div>`;
    };
    const workGroup = (title, cls, list) => list.length ? `
      <div class="wgroup">
        <div class="wgroup-head">
          <h3><span class="wg-name">${title}</span></h3>
          <span class="pill ${cls}">${list.length} 个作品</span>
          <span class="wg-line"></span>
        </div>
        <div class="work-grid">${list.map(workCardHtml).join("")}</div>
      </div>` : "";
    const act = items.filter((w) => (w.status || "draft") !== "archived").sort(byRecent);
    const done = items.filter((w) => (w.status || "") === "archived").sort(byRecent);
    // 分组渲染时外层不再是网格，由组内 .work-grid 承担布局
    grid.classList.remove("work-grid", "stagger");
    grid.innerHTML =
      workGroup("进行中", "amber", act) +
      workGroup("已归档", "gray", done);
  }
  grid.querySelectorAll(".work").forEach((card) => {
    const id = card.dataset.id;
    // I-3：整卡可点——除内嵌按钮外的任何区域都打开作品（键盘路径由「继续创作」按钮承担，卡片不再套 role=button）
    card.addEventListener("click", (e) => { if (e.target.closest("button")) return; openWork(id); });
    card.querySelector('[data-act="open"]').addEventListener("click", (e) => { e.stopPropagation(); openWork(id); });
    const archBtn = card.querySelector('[data-act="arch"]');
    if (archBtn) archBtn.addEventListener("click", (e) => { e.stopPropagation(); archiveWork(id); });
    card.querySelector('[data-act="del"]').addEventListener("click", (e) => { e.stopPropagation(); deleteWork(id); });
  });
  hydrateWorkCovers(grid);
}

/* I-9：无海报图的作品封面，用该作品素材库首张图懒加载填充；取不到则保持渐变 */
async function hydrateWorkCovers(grid) {
  if (!grid || !S.org) return;
  const jobs = Array.from(grid.querySelectorAll(".cover[data-cover-need]")).map(async (el) => {
    const wid = el.dataset.coverNeed;
    el.removeAttribute("data-cover-need");
    try {
      const list = V1(await ApiClient.get(`/api/v1/assets?org_id=${encodeURIComponent(S.org.id)}&work_id=${encodeURIComponent(wid)}`)) || [];
      const first = list.find((a) => (a.mime || "").startsWith("image/"));
      if (!first) return;
      // 与素材库缩略图同模式：走 /download 换签名 URL（/raw 为验签制接口，旧 Bearer 直调会静默 422、封面永远回退渐变）
      const d = V1(await ApiClient.get("/api/v1/assets/" + first.id + "/download?org_id=" + encodeURIComponent(S.org.id)));
      if (!d || !d.url) return;
      const im = document.createElement("img");
      im.className = "cover-img"; im.alt = ""; im.src = d.url;
      el.prepend(im);
    } catch (e) { /* 保持渐变回退 */ }
  });
  await Promise.allSettled(jobs);
}

/* V2.1：归档作品（后端 PATCH 已支持 status=archived，前端只加入口） */
async function archiveWork(id) {
  const w = S.works.find((x) => x.id === id);
  if (!w) return;
  if (!(await modalConfirm({ title: "归档作品", body: `确定归档作品「${w.title}」吗？归档后仍可在「已归档」筛选里查看。`, okText: "归档" }))) return;
  try {
    await ApiClient.patch("/api/v1/works/" + id, { status: "archived" });
    await loadWorks();
    renderWorkbench();
    toast("已归档");
  } catch (e) {
    toast("归档失败：" + (e.message || "网络错误"));
  }
}

async function newWorkFlow() {
  if (!loggedIn()) { go("v-login"); return; }
  const name = await modalInput({ title: "新建作品", placeholder: "请输入作品名称", value: "作品 " + (S.works.length + 1), okText: "创建" });
  if (name === null) return;
  try {
    await createWork(name);
    renderWorkbench();
    go("v2");
  } catch (e) {
    toast("创建失败：" + (e.message || "网络错误"));
  }
}

/* ================= v2 产品资料与事实库 ================= */
function collectProduct() {
  const name = $("p_name").value.trim();
  if (!name) { errText("product_err", "请填写产品名称"); return null; }
  errText("product_err", "");
  const points = $("p_points").value.split("\n").map((s) => s.trim()).filter(Boolean);
  return {
    product_name: name,
    description: [`品类：${$("p_category").value.trim()}`, `产地：${$("p_origin").value.trim()}`,
                  `规格：${$("p_spec").value.trim()}`, `销售方式：${$("p_channel").value.trim()}`]
                 .filter((s) => !s.endsWith("：")).join("；"),
    selling_points: points,
    price: $("p_price").value.trim(),
    audience: "",
    evidence_materials: "",
    shoot: { device: "手机", people: 1, lighting: "", location: "",
             target_duration_seconds: parseFloat($("p_duration").value) || 60, note: "" },
  };
}

function fillProductForm() {
  const p = (S.data && S.data.product) || {};
  $("p_name").value = p.product_name || "";
  $("p_price").value = p.price || "";
  $("p_points").value = (p.selling_points || []).join("\n");
  $("p_duration").value = (p.shoot && p.shoot.target_duration_seconds) || 60;
  // description 反填（品类/产地/规格/销售方式）
  const desc = p.description || "";
  const pick = (k) => { const m = desc.match(new RegExp(k + "：([^；]*)")); return m ? m[1] : ""; };
  $("p_category").value = pick("品类");
  $("p_origin").value = pick("产地");
  $("p_spec").value = pick("规格");
  $("p_channel").value = pick("销售方式");
  const title = p.product_name ? `${p.product_name} · 产品资料` : "产品资料";
  $("w2_title").textContent = title;
  ["w3_title", "w4_title", "w5_title", "w6_title"].forEach((id) => {
    const el = $(id);
    if (el && p.product_name) el.textContent = p.product_name;
  });
}

async function saveProduct() {
  if (!S.activeWork) { toast("请先创建作品"); return false; }
  const p = collectProduct();
  if (!p) return false;
  S.data.product = p;
  await saveWork();
  fillProductForm();
  toast("已保存");
  return true;
}

/* ---------- 事实库 ---------- */
const FACT_STATUS = {
  draft: ["草稿", "gray"], pending_review: ["待审核", "amber"], confirmed: ["已确认", "green"],
  rejected: ["已驳回", "red"], expired: ["已过期", "gray"], revoked: ["已撤销", "gray"],
};
function factUsable(f) { return f.status === "confirmed" && f.allow_use !== false; }
function usableFacts() {
  // V3.1 §3.1：带上 id，服务端 _check_fact_refs 才能按库核验
  // （confirmed+allow_use+未过期），无 id 的条目服务端会跳过
  return S.facts.filter(factUsable).map((f) => ({ id: f.id, content: f.content, category: f.category, source_ref: f.source_ref }));
}
function factsHash() {
  return usableFacts().map((f) => f.content).join("|");
}

async function loadFacts() {
  if (!S.org) return;
  let url = "/api/v1/facts?org_id=" + encodeURIComponent(S.org.id);
  // V3.0 B-3：选中产品档案时按产品筛选；未选中则与之前一致（不传参）
  if (S.selectedProductId) url += "&product_id=" + encodeURIComponent(S.selectedProductId);
  try {
    S.facts = V1(await ApiClient.get(url)) || [];
  } catch (e) {
    $("fact_list").innerHTML = `<p style="font-size:12.5px;color:var(--red)">加载失败：${esc(e.message || "网络错误")}</p>`;
    return;
  }
  renderFacts();
  checkFactsStale();
}

function renderFacts() {
  const list = $("fact_list");
  const confirmed = S.facts.filter((f) => f.status === "confirmed").length;
  const pending = S.facts.filter((f) => f.status === "pending_review").length;
  $("fact_summary").textContent = S.facts.length ? `已确认 ${confirmed} / 待审核 ${pending} / 共 ${S.facts.length}` : "";
  if (!S.facts.length) {
    list.innerHTML = emptyState("暂无事实", "添加已确认的产品信息，AI 生成时将优先使用有依据的内容。", true, { label: "补充事实", fn: () => { const b = $("btn_add_fact"); if (b) b.click(); } });
    return;
  }
  list.innerHTML = S.facts.map((f) => {
    const [label, tone] = FACT_STATUS[f.status] || [f.status, "gray"];
    const dot = f.status === "confirmed" ? "var(--green)" : f.status === "pending_review" ? "var(--amber)" : "#B9C2D0";
    let actions = "";
    if (f.status === "draft") actions = `<button class="btn ghost sm" data-fact="submit" data-id="${esc(f.id)}">提交审核</button>`;
    else if (f.status === "pending_review") actions = `<button class="btn primary sm" data-fact="confirm" data-id="${esc(f.id)}">确认</button> <button class="btn ghost sm" data-fact="reject" data-id="${esc(f.id)}">驳回</button>`;
    else if (f.status === "confirmed") actions = `<button class="btn ghost sm" data-fact="revoke" data-id="${esc(f.id)}">撤销</button>`;
    else if (f.status === "rejected" || f.status === "expired" || f.status === "revoked") actions = `<button class="btn ghost sm" data-fact="resubmit" data-id="${esc(f.id)}">重新提交</button>`;
    return `<div class="fact-row"><span class="dot" style="background:${dot}"></span>
      <div><b>${esc(f.content)}</b><span>分类：${esc(f.category || "—")}　来源：${esc(f.source_ref || "未填写")}　v${f.version || 1}${f.allow_use === false ? "　· 不用于生成" : ""}</span></div>
      ${f.status === "confirmed" ? '<span class="stamp-badge">已确认</span>' : `<span class="pill ${tone}">${label}</span>`}</div>
      <div style="display:flex;gap:8px;margin:2px 0 10px 18px;flex-wrap:wrap">
        ${actions}
        <button class="btn ghost sm" data-fact="edit" data-id="${esc(f.id)}">编辑内容</button>
        <button class="btn ghost sm" data-fact="history" data-id="${esc(f.id)}">变更记录</button>
      </div>`;
  }).join("");
  list.querySelectorAll("[data-fact]").forEach((b) => b.addEventListener("click", () => factAction(b.dataset.fact, b.dataset.id)));
}

async function factAction(act, id) {
  const f = S.facts.find((x) => x.id === id);
  if (!f) return;
  errText("fact_err", "");
  try {
    if (act === "submit" || act === "resubmit") {
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "pending_review", reason: "提交审核" });
    } else if (act === "confirm") {
      const reason = await modalInput({ title: "确认事实", placeholder: "确认理由（必填）", value: "已核实", required: true, okText: "下一步" });
      if (reason === null) return;
      if (!reason.trim()) { toast("确认需要填写理由"); return; }
      // 后端规则：确认事实必须设置 allow_use=true（作为宣传依据）
      if (!(await modalConfirm({ title: "允许用于 AI 生成", body: "确认后，这条事实将被标记为「允许用于 AI 生成」。继续吗？", okText: "确认" }))) return;
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "confirmed", reason: reason.trim(), allow_use: true });
    } else if (act === "reject") {
      const reason = await modalInput({ title: "驳回事实", placeholder: "驳回理由（必填）", required: true, okText: "驳回" });
      if (reason === null) return;
      if (!reason.trim()) { toast("驳回需要填写理由"); return; }
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "rejected", reason: reason.trim() });
    } else if (act === "revoke") {
      const reason = await modalInput({ title: "撤销事实", placeholder: "撤销理由（必填）", required: true, okText: "撤销" });
      if (reason === null) return;
      if (!reason.trim()) { toast("撤销需要填写理由"); return; }
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "revoked", reason: reason.trim() });
    } else if (act === "edit") {
      const content = await modalInput({ title: "编辑事实内容", body: "将生成新版本，旧版本保留", value: f.content, multiline: true, okText: "保存" });
      if (content === null || !content.trim() || content.trim() === f.content) return;
      await ApiClient.patch(`/api/v1/facts/${id}/content`, { org_id: S.org.id, expected_version: f.version, content: content.trim() });
      toast("已保存为新版本，旧版本可在变更记录查看");
    } else if (act === "history") {
      const h = V1(await ApiClient.get(`/api/v1/facts/${id}/history?org_id=` + encodeURIComponent(S.org.id))) || [];
      const txt = h.length ? h.map((x) => `${(x.created_at || "").slice(0, 16).replace("T", " ")}　${x.from_status || ""}→${x.to_status || ""}　${x.reason || ""}`).join("\n") : "暂无变更记录";
      await modalInfo({ title: `「${f.content.slice(0, 30)}…」的变更记录`, body: txt });
      return;
    }
    await loadFacts();
    if (act === "confirm") {
      const fb = $("fact_list")?.querySelector(`[data-fact="revoke"][data-id="${id}"]`);
      const frow = fb ? fb.closest("div")?.previousElementSibling : null;
      stampPop(frow && frow.classList.contains("fact-row") ? frow : (fb ? fb.closest("div") : null), "已确认");
    }
  } catch (e) {
    toast("操作失败：" + (e.message || "网络错误"));
  }
}

async function saveFact() {
  const content = $("f_content").value.trim();
  if (!content) { errText("fact_err", "请填写事实内容"); return; }
  try {
    const body = {
      org_id: S.org.id, content, category: $("f_category").value,
      source_type: "human_statement", source_ref: $("f_source").value.trim(),
    };
    // V3.0 B-3：选中产品档案时新建事实默认带入 product_id；未选中则与之前一致
    if (S.selectedProductId) body.product_id = S.selectedProductId;
    await ApiClient.post("/api/v1/facts", body);
    // 新建默认为草稿；若勾选允许用于生成，提交审核时再确认
    $("fact_editor").style.display = "none";
    $("f_content").value = ""; $("f_source").value = "";
    await loadFacts();
    toast("已添加（草稿），提交审核并确认后可用于 AI 生成");
  } catch (e) {
    errText("fact_err", e.message || "保存失败");
  }
}

/* ---------- 产品档案（V3.0 B-3：产品知识中心前端选择器） ---------- */
// 增量改造：只在 v2 视图加选择器，不改 facts 逻辑。未选中产品时行为与之前一致。

function ensureProductCard() {
  if ($("product_card")) return;
  const factsCard = $("fact_summary") && $("fact_summary").closest(".card");
  const card = document.createElement("div");
  card.className = "p2-prodsub"; card.id = "product_card";
  card.innerHTML = `<div class="p2-prodsub-head"><b>产品档案</b><span style="font-size:11.5px;color:var(--muted);font-weight:400" id="product_count"></span></div>
    <div class="p2-prodsub-row">
      <select id="product_select"><option value="">全部事实（不筛选）</option></select>
      <button class="btn ghost sm" id="btn_new_product">＋ 新建</button>
    </div>
    <div id="product_info" style="font-size:12.5px;color:var(--muted);line-height:1.7;margin-top:8px"></div>`;
  const anchor = factsCard && factsCard.querySelector("#fact_stale_warn");
  if (factsCard && anchor) factsCard.insertBefore(card, anchor);
  else if (factsCard) factsCard.appendChild(card);
  else document.getElementById("v2").appendChild(card);
  $("product_select").addEventListener("change", () => {
    S.selectedProductId = $("product_select").value;
    renderProductInfo();
    loadFacts(); // 选中后 facts 自动按 ?product_id= 筛选
  });
  $("btn_new_product").addEventListener("click", openProductModal);
}

async function loadProducts() {
  ensureProductCard();
  if (!S.org) { loadFacts(); return; }
  try {
    S.products = V1(await ApiClient.get("/api/v1/products?org_id=" + encodeURIComponent(S.org.id))) || [];
  } catch (e) {
    S.products = [];
    const sel = $("product_select");
    if (sel) { sel.disabled = true; }
    const info = $("product_info");
    if (info) info.innerHTML = `<span style="color:var(--red)">产品档案加载失败：${esc(e.message || "网络错误")}（可能缺少 product:read 权限）</span>`;
    loadFacts(); // 降级：不筛选，继续加载全部事实
    return;
  }
  const sel = $("product_select");
  sel.disabled = false;
  const keep = S.selectedProductId && S.products.some((p) => p.id === S.selectedProductId) ? S.selectedProductId : "";
  S.selectedProductId = keep;
  sel.innerHTML = `<option value="">全部事实（不筛选）</option>` + S.products.map((p) =>
    `<option value="${esc(p.id)}"${p.id === keep ? " selected" : ""}>${esc(p.name)}（${esc(p.code)}）</option>`).join("");
  $("product_count").textContent = S.products.length ? `共 ${S.products.length} 个` : "";
  renderProductInfo();
  loadFacts();
}

function renderProductInfo() {
  const info = $("product_info");
  if (!info) return;
  const p = S.products.find((x) => x.id === S.selectedProductId);
  if (!p) { info.textContent = S.selectedProductId ? "" : "不选则显示该组织全部事实；选中后事实列表只显示该产品，新建事实自动归属该产品。"; return; }
  const pts = (p.selling_points || []).slice(0, 3).join("、");
  info.innerHTML = `品类：${esc(p.category || "—")}　产地：${p.origin ? `<span class="region-tag">${esc(p.origin)}</span>` : "—"}${p.brand_tone ? `<br>品牌语气：${esc(p.brand_tone)}` : ""}${pts ? `<br>核心卖点：${esc(pts)}${(p.selling_points || []).length > 3 ? "…" : ""}` : ""}`;
}

/* ---------- V3.0 B-4：v3 生成页产品档案选择器 ---------- */
// 复用 B-3 的 S.products；独立状态 S.genProductId（空=不关联，不选也能用）。
// 关联后 genIdeas/genStoryboard 会把 product_id 传给后端，自动带入已确认事实。
async function ensureGenProductSelect() {
  const host = $("gen_product_host");
  if (!host) return;
  if (!S.org) { host.innerHTML = ""; return; }
  // 每次进入 v3 都刷新（B-5 过期判断依赖最新版本号；失败则保留旧列表降级）
  try {
    S.products = V1(await ApiClient.get("/api/v1/products?org_id=" + encodeURIComponent(S.org.id))) || [];
  } catch (e) { /* 保留旧列表 */ }
  const keep = (S.genProductId && S.products.some((p) => p.id === S.genProductId)) ? S.genProductId : "";
  S.genProductId = keep;
  if (!S.products.length) {
    host.innerHTML = `<span style="font-size:12.5px;color:var(--muted)">暂无产品档案（可在「产品资料」页新建），不影响生成。</span>`;
    return;
  }
  host.innerHTML = `<label style="font-size:13.5px;color:var(--muted)">关联产品档案（可选）
      <select id="gen_product_select" class="input sm" style="min-width:210px;margin-left:6px">
        <option value="">不关联（只用表单产品信息）</option>` +
    S.products.map((p) =>
      `<option value="${esc(p.id)}"${p.id === keep ? " selected" : ""}>${esc(p.name)}（${esc(p.code)}）·v${p.version || 1}</option>`).join("") +
    `</select></label>
    <span style="font-size:12.5px;color:var(--muted)">关联后自动带入该产品的已确认事实；未确认事实不会被当真</span>`;
  $("gen_product_select").addEventListener("change", (e) => { S.genProductId = e.target.value; });
}

/** 生成成功后记录产品引用（B-5 标记用）；resp 来自后端，含 product_ref。 */
function trackProductGenRef(resp) {
  if (resp && resp.product_ref && resp.product_ref.id) {
    const r = resp.product_ref;
    S.productGenRef = {
      product_id: r.id, product_version: r.version || 1,
      product_code: r.code || "", product_name: r.name || "",
    };
    const n = (resp.product_facts_used || []).length;
    if (n) toast(`已带入 ${n} 条已确认产品事实`);
  } else {
    // 本次生成未关联产品：清除旧引用（新内容不再基于产品档案）
    S.productGenRef = null;
  }
}

/** V3.0 B-5：产品引用是否已过期（版本对比，只做提示不改写内容）。 */
function productRefStaleInfo() {
  const ref = S.data && S.data.product_ref;
  if (!ref || !ref.product_id || !S.products || !S.products.length) return null;
  const p = S.products.find((x) => x.id === ref.product_id);
  if (!p) return null; // 产品已删或不可见：不打扰
  if ((p.version || 1) > (ref.product_version || 1)) return { ref, cur: p };
  return null;
}

function ensureProductModal() {
  if ($("product_modal")) return;
  const m = document.createElement("div");
  m.className = "modal-mask"; m.id = "product_modal";
  m.innerHTML = `<div class="modal" style="max-width:560px;max-height:88vh;overflow:auto"><h3>新建产品档案</h3>
    <div class="form-stack">
      <label class="f">产品编号 <span class="req">*</span><input id="np_code" placeholder="如：cherry-2026（组织内唯一，创建后不可改）"></label>
      <label class="f">产品名称 <span class="req">*</span><input id="np_name" placeholder="如：烟台大樱桃"></label>
      <label class="f">品类<input id="np_category" placeholder="如：水果"></label>
      <label class="f">产地<input id="np_origin" placeholder="如：山东烟台"></label>
      <label class="f">品牌资料<textarea id="np_brand_profile" rows="2" placeholder="品牌介绍、产地故事等"></textarea></label>
      <label class="f">品牌语气<input id="np_brand_tone" placeholder="如：实在、唠嗑、不端着"></label>
      <label class="f">产品卖点（每行一条）<textarea id="np_selling_points" rows="3" placeholder="果大汁多&#10;当季现摘"></textarea></label>
      <label class="f">宣传限制<textarea id="np_ad_restrictions" rows="2" placeholder="如：不得宣称有机认证"></textarea></label>
      <label class="f">禁止擅自扩展的信息<textarea id="np_no_expand" rows="2" placeholder="如：销量、排名、功效"></textarea></label>
    </div>
    <div class="err-text" id="product_archive_err"></div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn primary sm" id="btn_save_product_archive">保存</button>
      <button class="btn ghost sm" id="btn_cancel_product" style="margin-left:auto">取消</button>
    </div></div>`;
  document.body.appendChild(m);
  m.addEventListener("click", (e) => { if (e.target === m || e.target.id === "btn_cancel_product") m.classList.remove("show"); });
  $("btn_save_product_archive").addEventListener("click", saveProductArchive);
}

function openProductModal() {
  if (!S.org) { toast("请先选择组织"); return; }
  ensureProductModal();
  ["np_code", "np_name", "np_category", "np_origin", "np_brand_profile", "np_brand_tone", "np_selling_points", "np_ad_restrictions", "np_no_expand"].forEach((id) => { $(id).value = ""; });
  errText("product_archive_err", "");
  $("product_modal").classList.add("show");
}

async function saveProductArchive() {
  const code = $("np_code").value.trim();
  const name = $("np_name").value.trim();
  if (!code || !name) { errText("product_archive_err", "产品编号和产品名称必填"); return; }
  const selling_points = $("np_selling_points").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const created = V1(await ApiClient.post("/api/v1/products", {
      org_id: S.org.id, code, name,
      category: $("np_category").value.trim(), origin: $("np_origin").value.trim(),
      brand_profile: $("np_brand_profile").value.trim(), brand_tone: $("np_brand_tone").value.trim(),
      selling_points,
      ad_restrictions: $("np_ad_restrictions").value.trim(), no_expand_claims: $("np_no_expand").value.trim(),
    }));
    $("product_modal").classList.remove("show");
    await loadProducts();
    if (created && created.id) { // 新建成功后自动选中
      S.selectedProductId = created.id;
      $("product_select").value = created.id;
      renderProductInfo();
      loadFacts();
    }
    toast("产品档案已创建");
  } catch (e) {
    errText("product_archive_err", e.message || "保存失败");
  }
}

/* 事实变更只做过期提示，不覆盖旧成果 */
function checkFactsStale() {
  const h = factsHash();
  const warn = $("fact_stale_warn");
  if (!warn) return;
  const staleIdeas = (S.data.ideas || []).length && S.data.ideasFactsHash && S.data.ideasFactsHash !== h;
  const staleSb = S.data.storyboard && S.data.sbFactsHash && S.data.sbFactsHash !== h;
  if (staleIdeas || staleSb) {
    warn.innerHTML = `<div class="notice" style="margin:0 0 14px">产品事实已更新，之前生成的${[staleIdeas ? "创意方案" : "", staleSb ? "分镜" : ""].filter(Boolean).join("、")}可能基于旧资料，建议重新生成。旧成果已保留，不会被覆盖。</div>`;
  } else warn.innerHTML = "";
  const w3 = $("ideas_stale_warn");
  // V3.0 B-5：产品资料已更新、内容引用旧版本 → 只提示，不改写旧成果
  const pStale = productRefStaleInfo();
  const pMsg = pStale
    ? `<div class="notice" style="margin:0 0 14px">产品资料「${esc(pStale.ref.product_name || "产品档案")}」已更新（当前 v${pStale.cur.version || 1}），当前内容引用的是旧版本（v${pStale.ref.product_version || 1}）。建议核对后重新生成；旧成果已保留，不会被覆盖。</div>`
    : "";
  if (w3) w3.innerHTML = (staleIdeas ? `<div class="notice" style="margin:0 0 14px">产品事实已更新，这些创意可能基于旧资料，建议重新生成。</div>` : "") + pMsg;
  const wsb = $("sb_stale_warn");
  if (wsb) wsb.innerHTML = (staleSb ? `<div class="notice" style="margin:0 0 14px">产品事实已更新，此分镜可能基于旧资料，建议重新生成。</div>` : "") + pMsg;
}

/* ================= v3 创意策划与分镜 ================= */
const STAGES = [
  { v: "v2", label: "产品资料" }, { v: "v3", label: "创意策划" }, { v: "v3", label: "分镜编辑" },
  { v: "v5", label: "拍摄执行" }, { v: "v6", label: "交付" },
];
function renderStepper() {
  const d = S.data;
  const done = [!!(d.product && d.product.product_name), (d.ideas || []).length > 0,
                !!((d.storyboard || {}).shots || []).length, Object.keys(d.shoot_state || {}).length > 0, false];
  let cur = done.findIndex((x) => !x);
  if (cur < 0) cur = 4;
  if (cur < 1) cur = 1;
  $("stepper").innerHTML = STAGES.map((s, i) => {
    const cls = i < cur ? "done" : i === cur ? "cur" : "";
    const n = i < cur ? "✓" : String(i + 1);
    return `${i ? '<div class="stp-line"></div>' : ""}<div class="stp ${cls}"><span class="n">${n}</span>${s.label}</div>`;
  }).join("");
}

async function genIdeas() {
  if (!S.activeWork) { toast("请先创建作品"); return; }
  let p = collectProduct();
  if (!p) {
    // 用户可能直接从导航进入 v3，表单未填但作品里已有产品
    p = (S.data.product && S.data.product.product_name) ? S.data.product : null;
  }
  if (!p) { go("v2"); toast("请先填写产品资料"); return; }
  S.data.product = p;
  errText("ideas_err", "");
  const ideasBtnIds = ["btn_gen_ideas", "btn_regen_ideas"];
  setBtnsDisabled(ideasBtnIds, true);
  aiLoading("正在生成 3 个创意方向…");
  try {
    // V3.0 B-4：可选关联产品档案；关联后后端自动带入已确认事实
    const genPid = S.genProductId || "";
    const ideasBody = { product: p, facts: usableFacts() };
    if (genPid) { ideasBody.product_id = genPid; ideasBody.org_id = S.org.id; }
    const d = RAW(await ApiClient.post("/api/ideas", ideasBody,
      { idempotencyKey: "auto" }));
    S.data.ideas = d.ideas || [];
    S.data.ideasFactsHash = factsHash();
    trackProductGenRef(d); // V3.0 B-4/B-5：记录生成时的产品引用版本
    S.ideaSel = -1;
    await saveWork();
    renderIdeas();
    checkFactsStale();
    toast(`已生成 ${S.data.ideas.length} 个创意方向`);
  } catch (e) {
    errText("ideas_err", e.message || "生成失败");
  } finally {
    setBtnsDisabled(ideasBtnIds, false);
    aiLoadingDone();
  }
}

function renderIdeas() {
  const ideas = S.data.ideas || [];
  $("ideas_count").textContent = ideas.length ? `AI 已生成 ${ideas.length} 个` : "尚未生成";
  $("btn_regen_ideas").style.display = ideas.length ? "" : "none";
  const tabs = $("ideas_tabs"), cards = $("ideas_cards");
  if (!ideas.length) {
    tabs.style.display = "none";
    cards.innerHTML = '<p style="font-size:13.5px;color:var(--muted);grid-column:1/-1">先在「产品资料」页填写产品信息并保存，然后点击「生成创意方案」。AI 生成约需半分钟。</p>';
    return;
  }
  tabs.style.display = "";
  tabs.innerHTML = "";
  ideas.forEach((it, i) => {
    const b = document.createElement("button");
    b.className = "tab" + (i === S.ideaSel ? " active" : "");
    b.textContent = `方案 ${i + 1} · ${(it.name || "").slice(0, 8)}`;
    b.addEventListener("click", () => { S.ideaSel = i; renderIdeas(); });
    tabs.appendChild(b);
  });
  cards.innerHTML = ideas.map((it, i) => `
    <div class="idea-card ${i === S.ideaSel ? "sel" : ""}" data-i="${i}" tabindex="0" role="button">
      <b>${esc(it.name || ("方案 " + (i + 1)))}</b>
      <p>${esc(it.tagline || "")}</p>
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        <span class="pill blue">${esc(it.difficulty || "难度未知")}</span>
        ${it.fact_refs && it.fact_refs.length ? `<span class="pill gray">事实引用 ${it.fact_refs.length}</span>` : ""}
      </div>
      <div class="kv"><b>开场3秒</b><span>${esc(it.hook_3s || "—")}</span></div>
      <div class="kv"><b>核心思路</b><span>${esc(it.core_idea || "—")}</span></div>
      <div class="kv"><b>受众契合</b><span>${esc(it.audience_fit || "—")}</span></div>
    </div>`).join("");
  cards.querySelectorAll(".idea-card").forEach((c) => { keyActivate(c); c.addEventListener("click", async () => {
    S.ideaSel = +c.dataset.i;
    const it = ideas[S.ideaSel];
    S.data.direction = (it.name || "") + "：" + (it.tagline || "");
    await saveWork();
    renderIdeas();
    toast("已选择该方向，可点击「生成分镜」");
  }); });
}

/* ================= V2.0 P0：拍摄条件表单 + 可行性预检 ================= */
const VIDEO_PURPOSES = [["brand", "品牌宣传"], ["sales", "商品销售"], ["intro", "产品介绍"], ["record", "产地记录"]];
const SKILL_LEVELS = [["beginner", "新手"], ["skilled", "熟练"], ["pro", "专业"]];
const RESOURCES = ["手机支架", "补光灯", "背景布", "三脚架"];

function getShootCond() {
  const d = S.data.shoot_cond || {};
  return {
    video_purpose: d.video_purpose || "intro",
    skill_level: d.skill_level || "beginner",
    available_resources: d.available_resources || [],
    unavailable_scenes: d.unavailable_scenes || "",
  };
}

function initShootCondForm() {
  const btn = $("btn_gen_storyboard");
  if (!btn || $("shoot_cond_form")) return;
  const panel = btn.closest(".card.panel");
  const wrap = document.createElement("div");
  wrap.className = "card panel";
  wrap.style.marginBottom = "18px";
  wrap.id = "shoot_cond_form";
  const c = getShootCond();
  wrap.innerHTML = `
    <details ${c.video_purpose ? "" : "open"}>
      <summary style="cursor:pointer;font-weight:600;font-size:14px"><span class="cond-badge"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9.5 4.5a2.5 2.5 0 015 0"/><path d="M9.5 12.5l1.8 1.8 3.2-3.2"/></svg></span>拍摄条件 <span style="font-size:12.5px;color:var(--muted);font-weight:400">（生成分镜前确认，决定方案是否可行）</span></summary>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
        <label style="font-size:13.5px">视频用途<br>
          <select id="sc_purpose" style="width:100%;margin-top:4px;padding:8px;border-radius:8px;border:1px solid var(--line)">
            ${VIDEO_PURPOSES.map(([v, t]) => `<option value="${v}" ${c.video_purpose === v ? "selected" : ""}>${t}</option>`).join("")}
          </select></label>
        <label style="font-size:13.5px">拍摄熟练度<br>
          <select id="sc_skill" style="width:100%;margin-top:4px;padding:8px;border-radius:8px;border:1px solid var(--line)">
            ${SKILL_LEVELS.map(([v, t]) => `<option value="${v}" ${c.skill_level === v ? "selected" : ""}>${t}</option>`).join("")}
          </select></label>
      </div>
      <div style="margin-top:12px;font-size:13.5px">可用道具（多选）
        <div id="sc_res" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
          ${RESOURCES.map((r) => `<button type="button" data-res="${r}" class="pill ${c.available_resources.includes(r) ? "blue" : "gray"}" style="cursor:pointer;border:1px solid var(--line)">${r}</button>`).join("")}
        </div></div>
      <label style="display:block;margin-top:12px;font-size:13.5px">没有的场景（逗号分隔，如：没有果园，没有支架）
        <input id="sc_unavail" style="width:100%;margin-top:4px;padding:8px;border-radius:8px;border:1px solid var(--line)" placeholder="例如：没有果园" value="${esc(c.unavailable_scenes)}"></label>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn ghost sm" id="btn_check_feas2">检查可行性</button>
        <span id="sc_feas_result" style="font-size:13.5px"></span>
      </div>
      <div class="err-text" id="sc_err"></div>
    </details>`;
  panel.parentNode.insertBefore(wrap, panel);
  wrap.querySelectorAll("#sc_res button").forEach((b) => b.addEventListener("click", () => {
    b.classList.toggle("blue"); b.classList.toggle("gray"); saveShootCond(true);
  }));
  ["sc_purpose", "sc_skill", "sc_unavail"].forEach((id) => $(id).addEventListener("change", () => saveShootCond(true)));
  $("btn_check_feas2").addEventListener("click", checkFeasibilityV2);
}

async function saveShootCond(silent) {
  const res = [...document.querySelectorAll("#sc_res button.blue")].map((b) => b.dataset.res);
  S.data.shoot_cond = {
    video_purpose: $("sc_purpose").value,
    skill_level: $("sc_skill").value,
    available_resources: res,
    unavailable_scenes: $("sc_unavail").value.trim(),
  };
  if (!silent) await saveWork(); else saveWork();
}

async function checkFeasibilityV2() {
  await saveShootCond(false);
  const p = S.data.product || collectProduct();
  if (!p || !p.product_name) { errText("sc_err", "请先填写产品资料"); return; }
  errText("sc_err", "");
  const box = $("sc_feas_result");
  box.innerHTML = '<span style="color:var(--muted)">检查中…</span>';
  try {
    const c = getShootCond();
    const d = RAW(await ApiClient.post("/api/v2/feasibility-check", {
      product: p,
      shoot: {
        device: "手机", people: 1, lighting: "自然光",
        available_resources: c.available_resources,
        unavailable_scenes: c.unavailable_scenes.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        skill_level: c.skill_level,
      },
      video_purpose: c.video_purpose,
    }));
    const color = d.status === "ready" ? "green" : d.status === "need_confirm" ? "amber" : "red";
    const label = d.status === "ready" ? "条件已满足" : d.status === "need_confirm" ? "需要确认" : "需要替代方案";
    let html = `<span class="pill ${color}">${label}</span><div style="margin-top:8px">`;
    (d.checks || []).forEach((ch) => {
      html += `<div style="margin:6px 0;padding:8px;border:1px solid var(--line);border-radius:8px">
        <b>${esc(ch.item || "")}</b> — ${esc(ch.message || "")}
        ${ch.alternative ? `<br><span style="color:var(--accent)">替代：${esc(ch.alternative)}</span>` : ""}</div>`;
    });
    box.innerHTML = html + "</div>";
  } catch (e) {
    errText("sc_err", e.message || "检查失败");
    box.innerHTML = "";
  }
}

function pushSbVersion() {
  try {
    const key = "lupin_sb_versions";
    const arr = JSON.parse(localStorage.getItem(key) || "[]");
    if (S.data.storyboard) {
      arr.unshift({ ts: Date.now(), sb: JSON.parse(JSON.stringify(S.data.storyboard)) });
      localStorage.setItem(key, JSON.stringify(arr.slice(0, 3)));
    }
  } catch (e) { /* 忽略 */ }
}

async function restoreSbVersion() {
  try {
    const arr = JSON.parse(localStorage.getItem("lupin_sb_versions") || "[]");
    if (arr.length < 1) { toast("没有可恢复的版本"); return; }
    // arr[0] 是当前版，arr[1] 是上一版
    const prev = arr[1] || arr[0];
    if (!(await modalConfirm({ title: "恢复上一版分镜", body: "恢复到上一版分镜？当前修改将被覆盖。", okText: "恢复" }))) return;
    S.data.storyboard = prev.sb;
    saveWork(); renderShots();
    toast("已恢复上一版");
  } catch (e) { toast("恢复失败"); }
}

async function genStoryboard() {
  if (!S.activeWork) { toast("请先创建作品"); return; }
  const ideas = S.data.ideas || [];
  if (!ideas.length) { toast("请先生成创意方案"); return; }
  if (S.ideaSel < 0) { toast("请先点击选择一个创意方向"); return; }
  const it = ideas[S.ideaSel];
  const direction = (it.name || "") + "：" + (it.tagline || "");
  const p = S.data.product || collectProduct();
  if (!p || !p.product_name) { toast("请先填写产品资料"); go("v2"); return; }
  // V2.0：锁定保护 — 收集已锁定镜头
  const oldShots = (S.data.storyboard && S.data.storyboard.shots) || [];
  const locked = oldShots.filter((s) => s.locked);
  if (oldShots.length && locked.length && !(await modalConfirm({ title: "重新生成分镜", body: `有 ${locked.length} 个锁定镜头将保留，其余重新生成。继续？`, okText: "继续生成" }))) return;
  pushSbVersion();
  errText("sb_err", "");
  const sbBtnIds = ["btn_gen_storyboard", "btn_regen_all"];
  setBtnsDisabled(sbBtnIds, true);
  aiLoading("正在生成结构化分镜…（约半分钟）");
  try {
    const cond = getShootCond();
    // V3.0 B-4：可选关联产品档案；关联后后端自动带入已确认事实
    const genPid = S.genProductId || "";
    const sbBody = {
      product: p, direction, facts: usableFacts(),
      video_purpose: cond.video_purpose,
      available_resources: cond.available_resources,
    };
    if (genPid) { sbBody.product_id = genPid; sbBody.org_id = S.org.id; }
    const d = RAW(await ApiClient.post("/api/storyboard", sbBody, { idempotencyKey: "auto" }));
    let shots = d.storyboard.shots || [];
    // V2.0：恢复锁定镜头（按 shot_number 对位）
    if (locked.length) {
      const lockMap = {};
      locked.forEach((s) => { lockMap[s.shot_number] = s; });
      shots = shots.map((s) => lockMap[s.shot_number] || s);
    }
    d.storyboard.shots = shots;
    S.data.storyboard = d.storyboard;
    S.data.direction = direction;
    S.data.sbFactsHash = factsHash();
    trackProductGenRef(d); // V3.0 B-4/B-5：记录生成时的产品引用版本
    await saveWork();
    renderShots();
    checkFactsStale();
    toast(`已生成 ${shots.length} 个镜头${locked.length ? `（${locked.length} 个锁定已保留）` : ""}`);
  } catch (e) {
    errText("sb_err", e.message || "生成失败");
  } finally {
    setBtnsDisabled(sbBtnIds, false);
    aiLoadingDone();
  }
}

function shotDur(s) {
  // V2.0：优先用新版时长字段，老数据回退到 duration_seconds
  const rec = s.record_duration_seconds ?? s.duration_seconds ?? "—";
  const fin = s.final_duration_seconds ?? s.duration_seconds ?? "—";
  return `录制${rec}s / 成片${fin}s`;
}

function renderShots() {
  const sb = S.data.storyboard;
  const tb = $("sb_tbody");
  // V2.0：卡片式渲染（保留 tbody 容器，改放卡片）
  const wrap = $("sb_cards") || (() => {
    const d = document.createElement("div");
    d.id = "sb_cards";
    tb.closest("table").replaceWith(d);
    return d;
  })();
  if (!sb || !(sb.shots || []).length) {
    wrap.innerHTML = emptyState("暂无分镜", "先在上方选择创意方向，再生成分镜。", false, { label: "生成分镜", fn: () => { const b = $("btn_gen_storyboard"); if (b) b.click(); } });
    $("sb_meta").textContent = "";
    return;
  }
  const shots = sb.shots;
  const totalFin = shots.reduce((a, s) => a + (parseFloat(s.final_duration_seconds ?? s.duration_seconds) || 0), 0);
  $("sb_meta").textContent = `共 ${shots.length} 个镜头 · 成片约 ${Math.round(totalFin)} 秒`;
  wrap.innerHTML = shots.map((s, i) => {
    const steps = (s.shoot_steps || []).map((t, j) => `<li>${esc(t)}</li>`).join("");
    const acc = (s.acceptance_criteria || []).map((t) => `<li>✓ ${esc(t)}</li>`).join("");
    const fails = (s.failure_modes || []).map((f) => `<li><b>${esc(f.symptom || "")}</b> → ${esc(f.fix || "")}</li>`).join("");
    return `<div class="card" style="margin-bottom:12px;padding:14px;${s.locked ? "border:2px solid var(--accent);" : ""}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <b>镜头 ${s.shot_number || i + 1}</b>
        ${s.locked ? '<span class="pill blue"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="margin-right:3px"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>已锁定</span>' : ""}
        <span style="font-size:12.5px;color:var(--muted)">${esc(s.shot_type || "")} · ${shotDur(s)}</span>
        <span style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn ghost sm" data-shot="lock" data-i="${i}">${s.locked ? "解锁" : "锁定"}</button>
          <button class="btn ghost sm" data-shot="edit" data-i="${i}">编辑</button>
          <button class="btn ghost sm" data-shot="colloquial" data-i="${i}">拉呱版</button>
          <button class="btn ghost sm" data-shot="regen" data-i="${i}">重拍</button>
          <button class="btn ghost sm" data-shot="del" data-i="${i}">删除</button>
        </span>
      </div>
      ${s.shot_purpose ? `<p style="margin:8px 0;font-size:13.5px"><b>目的：</b>${esc(s.shot_purpose)}</p>` : ""}
      ${steps ? `<div style="font-size:13.5px"><b>拍摄步骤：</b><ol style="margin:4px 0;padding-left:20px">${steps}</ol></div>`
              : (s.shooting_action ? `<p style="font-size:13.5px"><b>动作：</b>${esc(s.shooting_action)}</p>` : "")}
      ${acc ? `<div style="font-size:13.5px"><b>验收标准：</b><ul style="margin:4px 0;padding-left:20px;color:var(--ok)">${acc}</ul></div>` : ""}
      <details style="margin-top:8px;font-size:12.5px;color:var(--muted)">
        <summary style="cursor:pointer">机位 / 运镜 / 失败处理 / 旁白字幕</summary>
        <div style="margin-top:6px;display:grid;gap:6px">
          ${s.phone_setup ? `<div><b>机位：</b>${esc(s.phone_setup)}</div>` : ""}
          ${s.camera_movement ? `<div><b>运镜：</b>${esc(s.camera_movement)}</div>` : ""}
          ${s.scene_description ? `<div><b>画面：</b>${esc(s.scene_description)}</div>` : ""}
          ${fails ? `<div><b>失败处理：</b><ul style="margin:4px 0;padding-left:18px">${fails}</ul></div>` : ""}
          ${s.alt_approach ? `<div><b>替代拍法：</b>${esc(s.alt_approach)}</div>` : ""}
          ${s.reshoot_guide ? `<div><b>补拍指引：</b>${esc(s.reshoot_guide)}</div>` : ""}
          ${(s.voiceover || s.subtitle) ? `<div><b>旁白：</b>${esc(s.voiceover || "—")}<br><b>字幕：</b>${esc(s.subtitle || "—")}</div>` : ""}
        </div>
      </details>
      <div class="shot-edit" data-edit="${i}" style="display:none">
        <div class="form-stack">
          <label class="f">画面描述<textarea class="se-scene" rows="2">${esc(s.scene_description || "")}</textarea></label>
          <label class="f">台词/口播<input class="se-vo" value="${esc(s.voiceover || "")}"></label>
          <label class="f">时长（秒）<input class="se-dur" type="number" step="0.5" min="0" value="${esc(String(s.duration_seconds ?? ""))}"></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn primary sm" data-edit-save="${i}">保存</button>
          <button class="btn ghost sm" data-edit-cancel="${i}">取消</button>
        </div>
      </div>
    </div>`;
  }).join("") + `<div style="margin-top:8px"><button class="btn ghost sm" id="btn_restore_sb">↩ 恢复上一版</button></div>`;
  wrap.querySelectorAll("[data-shot]").forEach((b) => b.addEventListener("click", () => shotAction(b.dataset.shot, +b.dataset.i)));
  wrap.querySelectorAll("[data-edit-save]").forEach((b) => b.addEventListener("click", () => saveShotEdit(+b.dataset.editSave)));
  wrap.querySelectorAll("[data-edit-cancel]").forEach((b) => b.addEventListener("click", () => {
    const box = wrap.querySelector(`[data-edit="${b.dataset.editCancel}"]`);
    if (box) box.style.display = "none";
  }));
  const rb = $("btn_restore_sb");
  if (rb) rb.addEventListener("click", restoreSbVersion);
}

/* 镜头行内编辑保存（Phase2：替代原三连 prompt，逻辑与原先一致） */
async function saveShotEdit(i) {
  const shots = S.data.storyboard.shots;
  const s = shots[i];
  const box = document.querySelector(`[data-edit="${i}"]`);
  if (!s || !box) return;
  const oldVo = s.voiceover || "";
  s.scene_description = box.querySelector(".se-scene").value.trim();
  s.voiceover = box.querySelector(".se-vo").value.trim();
  const durRaw = box.querySelector(".se-dur").value.trim();
  const d = parseFloat(durRaw);
  if (durRaw && !(d > 0)) { toast("时长需大于 0 秒，已保留原时长"); }
  else if (d > 0) s.duration_seconds = d;
  if (s.voiceover !== oldVo) { markRiskDirty(); } // V2.1：口播文案变更 → 风险检查标脏
  await saveWork(); renderShots();
  updateRiskDirtyBadge();
  toast("已更新");
}

async function shotAction(act, i) {
  const shots = S.data.storyboard.shots;
  const s = shots[i];
  if (act === "lock") {
    s.locked = !s.locked;
    await saveWork(); renderShots();
    toast(s.locked ? `镜头 ${s.shot_number} 已锁定，重新生成时将保留` : `镜头 ${s.shot_number} 已解锁`);
  } else if (act === "del") {
    if (!(await modalConfirm({ title: "删除镜头", body: `删除镜头 ${s.shot_number}？删除后可点「恢复上一版」找回。`, okText: "删除", danger: true }))) return;
    pushSbVersion();
    shots.splice(i, 1);
    shots.forEach((x, j) => { x.shot_number = j + 1; });
    await saveWork(); renderShots();
  } else if (act === "edit") {
    // Phase2：行内展开表单（表单在 renderShots 模板内，保存走 saveShotEdit）；P-5：再点一次「编辑」收起（toggle）
    const box = document.querySelector(`[data-edit="${i}"]`);
    const wasOpen = box && box.style.display !== "none";
    document.querySelectorAll(".shot-edit").forEach((el) => { el.style.display = "none"; });
    if (box && !wasOpen) { box.style.display = ""; const f = box.querySelector(".se-scene"); if (f) f.focus(); }
  } else if (act === "colloquial") {
    openColloquialModal(i);
  } else if (act === "regen") {
    const instruction = await modalInput({ title: `重生成镜头 ${s.shot_number}`, placeholder: "修改要求（例如：改成俯视角度）", required: true, okText: "重生成" });
    if (!instruction || !instruction.trim()) return;
    aiLoading(`正在重生成镜头 ${s.shot_number}…`);
    try {
      const d = RAW(await ApiClient.post("/api/regenerate-shot", {
        storyboard: S.data.storyboard, shot_number: s.shot_number,
        instruction: instruction.trim(), locked: [],
      }));
      S.data.storyboard = d.storyboard;
      markRiskDirty(); // V2.1：重生成改变镜头文案 → 风险检查标脏
      await saveWork(); renderShots();
      updateRiskDirtyBadge();
      if (d.warning) toast("注意：" + d.warning); else toast("已重生成");
    } catch (e) {
      toast("重生成失败：" + (e.message || "网络错误"));
    } finally {
      aiLoadingDone();
    }
  }
}

async function addShot() {
  const sb = S.data.storyboard;
  if (!sb) { toast("请先生成分镜"); return; }
  const scene = await modalInput({ title: "新增镜头", placeholder: "新镜头的画面描述", required: true, okText: "下一步" });
  if (!scene || !scene.trim()) return;
  const durStr = await modalInput({ title: "镜头时长", placeholder: "时长（秒）", value: "5", okText: "添加" });
  if (durStr === null) return; // I-5：第二步取消即放弃新增，不能把取消吞成默认值继续创建
  const dur = parseFloat(durStr) || 5;
  sb.shots.push({ shot_number: sb.shots.length + 1, shot_type: "中景", scene_description: scene.trim(),
    shooting_action: "", camera_movement: "", voiceover: "", subtitle: "", duration_seconds: dur, props: [], required_assets: [] });
  await saveWork(); renderShots();
}

/* ---------- 风险 / 可行性 ---------- */
/* ---------- V2.1：文案修改后风险检查标脏 ---------- */
function markRiskDirty() {
  // 只有"风险检查已运行过"时才标脏；没跑过的不需要
  if (S.data && S.data.risk != null && !S.data.risk_dirty) {
    S.data.risk_dirty = true;
  }
}
function updateRiskDirtyBadge() {
  const el = $("risk_dirty_badge");
  if (el) el.style.display = (S.data && S.data.risk_dirty && S.data.risk != null) ? "" : "none";
}

async function runRisk() {
  const sb = S.data.storyboard;
  if (!sb) { toast("请先生成分镜"); return; }
  errText("risk_err", "");
  aiLoading("正在做风险检查…");
  try {
    const p = S.data.product || {};
    const d = RAW(await ApiClient.post("/api/risk-check", {
      storyboard: sb, evidence_materials: p.evidence_materials || "",
      selling_points: p.selling_points || [], product: p, facts: usableFacts(),
    }));
    S.data.risk = (d.flags || []).map((f) => ({ ...f, confirmed: false }));
    S.data.risk_dirty = false; // V2.1：重新运行后清除标脏
    await saveWork();
    renderRisks();
    updateRiskDirtyBadge();
    if (!S.data.risk.length) {
      // V2.0：文案从后端统一接口取，不再硬编码
      try {
        const t = RAW(await ApiClient.get("/api/v2/risk-text"));
        $("risk_results").innerHTML = `<p style="font-size:13.5px;color:var(--muted)">${esc(t.no_risk_found_text || "")}</p>`;
      } catch (e) {
        $("risk_results").innerHTML = '<p style="font-size:13.5px;color:var(--muted)">本脚本未发现需要标注的风险断言，发布前请人工确认待确认事项。本工具不保证广告合规。</p>';
      }
    }
  } catch (e) {
    errText("risk_err", e.message || "检查失败");
  } finally {
    aiLoadingDone();
  }
}

function renderRisks() {
  const risks = S.data.risk || [];
  if (!risks.length) return;
  const lv = { "高": "red", "中": "amber", "低": "green" };
  const items = risks.map((f, i) => `
    <div class="risk-item">
      <input type="checkbox" data-ri="${i}" ${f.confirmed ? "checked" : ""} title="人工确认">
      <div><div><span class="pill ${lv[f.level] || "gray"}">${esc(f.level || "")}风险</span>
      <span class="pill gray">${esc(f.source || "")}</span> ${esc(f.text || "")}</div>
      <div style="font-size:12.5px;color:var(--muted);margin-top:4px">${esc(f.reason || "")}</div></div>
    </div>`).join("");
  // V3.1 P1-8：标脏后旧结论不得再显示为"当前有效"——折叠为"过期结论（仅供参考）"，
  // 历史保留可查；重新运行风险检查后恢复正常展示
  const body = S.data.risk_dirty
    ? `<div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:10px;padding:10px 12px;margin-bottom:10px;font-size:12.5px">⚠ 文案已修改，以下为<strong>过期结论</strong>，不代表当前文案的风险状况，请重新运行风险检查。</div>
       <details><summary style="font-size:12.5px;color:var(--muted);cursor:pointer">查看过期结论（${risks.length} 条，仅供参考）</summary><div style="opacity:.65;margin-top:8px">${items}</div></details>`
    : items;
  $("risk_results").innerHTML = body;
  $("risk_results").querySelectorAll("[data-ri]").forEach((c) => c.addEventListener("change", async () => {
    S.data.risk[+c.dataset.ri].confirmed = c.checked;
    await saveWork();
  }));
}

async function runFeas() {
  const sb = S.data.storyboard;
  if (!sb) { toast("请先生成分镜"); return; }
  errText("feas_err", "");
  aiLoading("正在检查拍摄可行性…");
  try {
    const d = RAW(await ApiClient.post("/api/feasibility-check", { storyboard: sb }));
    S.data.feasibility = d.results || [];
    await saveWork();
    renderFeas(d);
  } catch (e) {
    errText("feas_err", e.message || "检查失败");
  } finally {
    aiLoadingDone();
  }
}

function renderFeas(d) {
  const results = (d && d.results) || S.data.feasibility || [];
  if (!results.length) return;
  const tone = { "可执行": "feas-ok", "建议调整": "feas-warn", "不可执行": "feas-bad" };
  const counts = {};
  results.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
  $("feas_results").innerHTML =
    `<p style="font-size:13.5px;color:var(--muted);margin-bottom:12px">共 ${results.length} 镜头：` +
    ["可执行", "建议调整", "不可执行", "需要人工确认"].map((s) => `${s} ${counts[s] || 0}`).join(" · ") + `</p>` +
    `<p style="font-size:12.5px;color:var(--muted)">本检查为拍摄可行性提示，不构成绝对保证；最终以实际拍摄条件为准。</p>` +
    results.map((r) => `
    <div class="risk-item ${tone[r.status] || ""}">
      <div style="flex:1"><div><b>镜头 ${r.shot_number}</b> <span class="pill ${r.status === "可执行" ? "green" : r.status === "不可执行" ? "red" : "amber"}">${esc(r.status)}</span>
      <span class="pill gray">${esc(r.source || "")}判断</span></div>
      <div style="font-size:12.5px;color:var(--ink2);margin-top:4px">${esc(r.reason || "")}</div>
      ${(r.constraints_involved || []).length ? `<div style="font-size:12.5px;color:var(--muted)">涉及约束：${esc(r.constraints_involved.join("、"))}</div>` : ""}
      ${r.alternative ? `<div style="font-size:12.5px;margin-top:6px;background:var(--bg);border-radius:8px;padding:10px">
        <b>替代方案：</b>${esc(r.alternative.shooting_method || "")}　${esc(r.alternative.device_usage || "")}　${esc(r.alternative.camera_position || "")}</div>` : ""}
      </div></div>`).join("");
}

/* ================= v4 海报制作 ================= */
const POSTER_TPLS = {
harvest: { name: "丰收橙", bg: "linear-gradient(160deg,#6E2A18 0%,#9E4423 45%,#BC6E2B 80%,#C07F33 100%)"},
ocean: { name: "海洋蓝", bg: "linear-gradient(160deg,#12395E 0%,#1B4D6B 50%,#2E7BB0 100%)"},
orchard: { name: "果园绿", bg: "linear-gradient(160deg,#1E3A24 0%,#2C4A2E 50%,#5E9E52 100%)"},
grape: { name: "葡萄紫", bg: "linear-gradient(160deg,#3A1E5E 0%,#5B2A86 50%,#9E6BD8 100%)"},
};
function posterState() {
const p = (S.data && S.data.poster) || {};
const prod = (S.data && S.data.product) || {};
return {
tpl: p.tpl || "harvest",
title: p.title || prod.product_name || "",
sub: p.sub || "",
promo: p.promo || "",
btn: p.btn || "立即抢购",
copy: p.copy || "",
ai_image: p.ai_image || "",
};
}
function renderPoster() {
const st = posterState();
$("pe_title").value = st.title;
$("pe_sub").value = st.sub;
$("pe_promo").value = st.promo;
$("pe_btn").value = st.btn;
$("pe_copy").value = st.copy;
checkImageStatus();
document.querySelectorAll("#poster_tpls .t").forEach((t) => t.classList.toggle("active", t.dataset.tpl === st.tpl));
updatePosterPreview();
}
function updatePosterPreview() {
const tpl = document.querySelector("#poster_tpls .t.active");
const key = tpl? tpl.dataset.tpl: "harvest";
const T = POSTER_TPLS[key] || POSTER_TPLS.harvest;
const pv = $("poster_preview");
pv.style.background = T.bg;
pv.classList.toggle("light", !!T.light);
pv.style.aspectRatio = T.horizontal ? "16/10" : "";
pv.style.minHeight = T.horizontal ? "0" : "";
pv.style.borderWidth = T.light ? "3px" : "";
pv.style.borderColor = T.light ? "#D4A05A" : "";
let aiBg = pv.querySelector(".ai-bg"), scrim = pv.querySelector(".ai-scrim");
const aiSrc = (posterState() || {}).ai_image;
if (aiSrc) {
if (!aiBg) { aiBg = document.createElement("img"); aiBg.className = "ai-bg"; aiBg.alt = ""; pv.prepend(aiBg); }
aiBg.src = aiSrc;
if (!scrim) { scrim = document.createElement("div"); scrim.className = "ai-scrim"; aiBg.after(scrim); }
} else {
if (aiBg) aiBg.remove();
if (scrim) scrim.remove();
}
const title = $("pe_title").value.trim() || "产品名称";
const sub = $("pe_sub").value.trim();
$("pp_title").innerHTML = esc(title).replace(/\n/g, "<br>") + (sub? "<br>" + esc(sub): "");
$("pp_promo").textContent = $("pe_promo").value.trim() || "促 销 信 息";
$("pp_btn").textContent = ($("pe_btn").value.trim() || "行动按钮") + " →";
}
function collectPoster() {
const tpl = document.querySelector("#poster_tpls .t.active");
const prev = (S.data && S.data.poster) || {};
return { tpl: tpl? tpl.dataset.tpl: "harvest", title: $("pe_title").value.trim(),
sub: $("pe_sub").value.trim(), promo: $("pe_promo").value.trim(),
btn: $("pe_btn").value.trim(), copy: $("pe_copy").value.trim(),
ai_image: prev.ai_image || ""};
}
async function savePosterDraft() {
if (!S.activeWork) { toast("请先创建作品"); return;}
S.data.poster = collectPoster();
await saveWork();
$("poster_msg").textContent = "✓ 草稿已保存到当前作品";
setTimeout(() => { $("poster_msg").textContent = "";}, 2500);
}
async function exportPosterPNG() {
const st = collectPoster();
const T = POSTER_TPLS[st.tpl] || POSTER_TPLS.harvest;
const horizontal = !!T.horizontal, light = !!T.light;
const W = horizontal ? 1920 : 1080, H = horizontal ? 1080 : 1920;
const cv = document.createElement("canvas");
cv.width = W; cv.height = H;
const ctx = cv.getContext("2d");
// 背景：AI 图优先（cover），否则按模板取首尾色做竖向渐变
let drewImg = false;
if (st.ai_image) drewImg = await drawCoverImage(ctx, st.ai_image, W, H);
if (!drewImg) {
const colorStops = { harvest: ["#6E2A18", "#C07F33"], ocean: ["#12395E", "#2E7BB0"],
orchard: ["#1E3A24", "#5E9E52"], grape: ["#3A1E5E", "#9E6BD8"],
guochao: ["#0C4229", "#7A1F1A"], minimal_h: ["#F8F4EA", "#EFE7D2"]};
const [c1, c2] = colorStops[st.tpl] || colorStops.harvest;
const g = ctx.createLinearGradient(0, 0, 0, H);
g.addColorStop(0, c1); g.addColorStop(1, c2);
ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
}
// AI 图或深色模板：加底部暗角保证文字可读
if (drewImg || !light) {
const sg = ctx.createLinearGradient(0, H * 0.35, 0, H);
sg.addColorStop(0, "rgba(8,25,18,0)"); sg.addColorStop(1, "rgba(8,25,18,.72)");
ctx.fillStyle = sg; ctx.fillRect(0, 0, W, H);
}
// 边框（与页面预览一致）
ctx.strokeStyle = light ? "#D4A05A" : "#C62828"; ctx.lineWidth = 28;
ctx.strokeRect(14, 14, W - 28, H - 28);
const ink = light && !drewImg, ty = H - (horizontal ? 330 : 620);
ctx.fillStyle = ink ? "#252820" : "#fff"; ctx.textAlign = "center";
const title = (st.title || "产品名称") + (st.sub? "\n" + st.sub: "");
ctx.font = "900 " + (horizontal ? 96 : 110) + "px 'Noto Sans SC', sans-serif";
const lines = title.split("\n");
lines.forEach((ln, i) => ctx.fillText(ln, W / 2, ty + i * (horizontal ? 120 : 140)));
ctx.font = "500 44px 'Noto Sans SC', sans-serif";
ctx.fillStyle = ink ? "#5B5F52" : "#FFF6E3";
ctx.fillText((st.promo || "促销信息").split("").join(" "), W / 2, ty + (horizontal ? 170 : 240));
// 按钮
const bw = 420, bh = 110, bx = (W - bw) / 2, by = ty + (horizontal ? 230 : 300);
ctx.fillStyle = ink ? "#0F5132" : "#fff";
ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 55); ctx.fill();
ctx.fillStyle = ink ? "#fff" : "#B4502E"; ctx.font = "700 48px 'Noto Sans SC', sans-serif";
ctx.fillText((st.btn || "立即抢购") + " →", W / 2, by + 74);
const a = document.createElement("a");
a.href = cv.toDataURL("image/png");
a.download = `海报_${st.title || "未命名"}.png`;
document.body.appendChild(a); a.click(); a.remove();
toast("海报 PNG 已导出");
}

/* ================= v5 拍摄执行（V2.0 P0：五态） ================= */
function shootKey(s, i) {
  // V2.0：key 加入画面 hash，避免分镜重排后状态错位
  const h = (s.scene_description || "").slice(0, 20);
  let x = 0;
  for (let j = 0; j < h.length; j++) x = ((x << 5) - x + h.charCodeAt(j)) | 0;
  return "s" + (s.shot_number || i + 1) + "_" + Math.abs(x).toString(36);
}
// V2.1：六态 + 转换约束。点击卡片进入转换表中的第一个合法状态，不再无脑循环。
// 待拍摄 → 拍摄中、用替代方案
const SHOOT_STATES = [
  ["todo", "待拍摄", "amber"],
  ["shooting", "拍摄中", "blue"],
  ["shot", "已拍摄·待验收", "blue"],
  ["passed", "验收通过", "green"],
  ["reshoot", "需要补拍", "red"],
  ["alt", "用替代方案", "gray"],
];
const SHOOT_TRANSITIONS = {
  todo: ["shooting", "alt"],
  shooting: ["shot", "alt"],
  shot: ["passed", "reshoot", "alt"],
  reshoot: ["shooting", "alt"],
  passed: ["reshoot", "alt"],
  alt: ["shooting", "todo"],
};
function nextShootState(cur) {
  const allowed = SHOOT_TRANSITIONS[cur];
  if (allowed && allowed.length) return allowed[0];
  return "todo";
}
function renderShoot() {
const sb = S.data.storyboard;
const list = $("shoot_list");
if (!sb ||!(sb.shots || []).length) {
list.innerHTML = emptyState("暂无分镜", "先去创意策划生成分镜，再回来逐镜拍摄。", false, { label: "去创意策划", fn: () => go("v3") });
$("shoot_progress").textContent = "拍摄进度";
$("shoot_bar").style.width = "0%"; $("shoot_pct").textContent = "0%";
return;
}
const shots = sb.shots;
const st = S.data.shoot_state || {};
let shotCount = 0, passCount = 0;
list.innerHTML = shots.map((s, i) => {
const k = shootKey(s, i);
const cur = (st[k] || {}).status || "todo";
if (cur === "shot" || cur === "passed" || cur === "reshoot" || cur === "alt") shotCount++;
if (cur === "passed") passCount++;
const meta = SHOOT_STATES.find(([v]) => v === cur) || SHOOT_STATES[0];
const acc = (s.acceptance_criteria || []).slice(0, 3).map((t) => `<li>${esc(t)}</li>`).join("");
const accRec = getAcc(k);
const accMeta = accRec ? ACC_LABELS[accRec.status] : null;
const assetN = (accRec && accRec.asset_ids) ? accRec.asset_ids.length : 0;
return `<div class="shot-card" data-k="${k}" data-i="${i}" style="border-bottom:1px solid #EFE9D8">
<div class="check" tabindex="0" role="button" aria-label="切换镜头 ${s.shot_number || i + 1} 拍摄状态" style="cursor:pointer;border:0;padding-bottom:6px">
<span class="box" style="${cur === "passed" ? "background:var(--ok);" : ""}"></span>
<span class="t"><b>镜头 ${s.shot_number || i + 1} · ${esc(s.shot_purpose || s.scene_description || "")}</b><br>
<span style="color:var(--muted);font-size:12.5px">${shotDur(s)} · ${esc((s.shoot_steps || [])[0] || s.shooting_action || "")}</span>
${acc ? `<ul style="margin:4px 0;padding-left:18px;font-size:12.5px;color:var(--ok)">✓验收：${acc}</ul>` : ""}
${accMeta ? `<span class="pill ${accMeta[1]}" style="font-size:11.5px;margin-top:4px;display:inline-block">验收：${accMeta[0]}</span>` : ""}</span>
<span class="note pill ${meta[2]}">${meta[1]}</span></div>
<div class="acc-bar" style="display:flex;gap:8px;align-items:center;padding:0 6px 12px 38px;flex-wrap:wrap">
<button class="btn ghost sm" data-acc="accepted" style="font-size:12.5px;padding:5px 12px">验收通过</button>
<button class="btn ghost sm" data-acc="needs_reshoot" style="font-size:12.5px;padding:5px 12px">需补拍</button>
<button class="btn ghost sm" data-acc="pending" style="font-size:12.5px;padding:5px 12px">待定</button>
<button class="btn ghost sm" data-asset="1" style="font-size:12.5px;padding:5px 12px">关联素材（${assetN}）</button>
</div></div>`;
}).join("");
list.querySelectorAll(".shot-card").forEach((c) => {
const k = c.dataset.k, i = +c.dataset.i;
const s = S.data.storyboard.shots[i];
const chk = c.querySelector(".check");
keyActivate(chk);
chk.addEventListener("click", async () => {
const cur = ((S.data.shoot_state || {})[k] || {}).status || "todo";
const nxt = nextShootState(cur);
// V2.1：用替代方案必须填写原因，取消则中止切换
let note = "";
if (nxt === "alt") {
  const reason = await modalInput({ title: "使用替代方案", placeholder: "请填写使用替代方案的原因", okText: "确定" });
  if (reason === null) return;
  note = (reason || "").trim();
}
S.data.shoot_state[k] = { status: nxt, note };
await saveWork();
renderShoot();
if (nxt === "reshoot" && s.reshoot_guide) {
  await modalInfo({ title: `镜头 ${s.shot_number} 补拍指引`, body: s.reshoot_guide });
} else if (nxt === "alt") {
  toast("已记录替代方案" + (s.alt_approach ? "：" + s.alt_approach.slice(0, 40) : ""));
}
});
/* V2.0 P1：验收按钮组（阻止冒泡，不触发状态切换） */
c.querySelectorAll("[data-acc]").forEach((b) => b.addEventListener("click", (e) => {
e.stopPropagation();
submitAcceptance(k, s.shot_number || i + 1, b.dataset.acc, b);
}));
const ab = c.querySelector("[data-asset]");
if (ab) ab.addEventListener("click", (e) => {
e.stopPropagation();
pickShotAsset(k, s.shot_number || i + 1);
});
});
// V2.0：拍摄进度 vs 验收进度两个指标
const pctShot = shots.length ? Math.round((shotCount / shots.length) * 100) : 0;
const pctPass = shots.length ? Math.round((passCount / shots.length) * 100) : 0;
$("shoot_progress").textContent = `拍摄 ${shotCount}/${shots.length} · 验收 ${passCount}/${shots.length}`;
$("shoot_bar").style.width = pctPass + "%";
$("shoot_pct").textContent = `验收 ${pctPass}%`;
renderLivestream();
resumeLivestreamTask(); // V3.1 P1-4：刷新页面后恢复未完成的生成任务
}

/* ================= v5 拍摄验收 + 素材关联（V2.0 P1） ================= */
const ACC_LABELS = {
accepted: ["验收通过", "green"],
needs_reshoot: ["需补拍", "red"],
pending: ["待验收", "amber"],
};
function getAcc(k) { return (S.data.shot_acceptance || {})[k] || null; }

/* PATCH /api/v2/shoot/works/{id}/shots/accept：asset_ids 为全量覆盖语义 */
async function submitAcceptanceRaw(k, shotNumber, status, note, assetIds, btn) {
if (!S.activeWork) { toast("请先创建作品"); return; }
S._accBusy = S._accBusy || {};
if (S._accBusy[k]) return;
S._accBusy[k] = true;
if (btn) btn.disabled = true;
try {
const r = await ApiClient.patch(`/api/v2/shoot/works/${S.activeWork.id}/shots/accept`, {
shot_key: k, shot_number: shotNumber, status: status, note: note || "", asset_ids: assetIds || [],
});
const res = RAW(r) || {};
S.data.shot_acceptance = S.data.shot_acceptance || {};
if (res.acceptance) S.data.shot_acceptance[res.shot_key || k] = res.acceptance;
await saveWork();
renderShoot();
toast(status === "accepted" ? "已验收通过" : status === "needs_reshoot" ? "已标记需补拍" : "已设为待验收");
} catch (e) {
toast("验收提交失败：" + (e.message || "网络错误"));
} finally {
S._accBusy[k] = false;
if (btn) btn.disabled = false;
}
}
async function submitAcceptance(k, shotNumber, status, btn) {
let note = "";
if (status === "needs_reshoot") {
note = await modalInput({ title: "标记需补拍", placeholder: "请简要描述问题（会出现在补拍清单里）", okText: "提交" });
if (note === null) return;
}
const cur = getAcc(k);
await submitAcceptanceRaw(k, shotNumber, status, note || "", (cur && cur.asset_ids) || [], btn);
}

/* 关联素材：前端先验大小/类型，上传后取 asset id 并追加进该镜头 asset_ids */
let _shotFileInput = null, _shotFileKey = null, _shotFileNum = null;
function pickShotAsset(k, shotNumber) {
if (!S.activeWork) { toast("请先创建作品"); return; }
if (!_shotFileInput) {
_shotFileInput = document.createElement("input");
_shotFileInput.type = "file"; _shotFileInput.accept = "image/*,video/*"; _shotFileInput.style.display = "none";
document.body.appendChild(_shotFileInput);
_shotFileInput.addEventListener("change", () => {
const f = _shotFileInput.files[0]; _shotFileInput.value = "";
const kk = _shotFileKey, nn = _shotFileNum; _shotFileKey = null;
if (f && kk) uploadShotAsset(kk, nn, f);
});
}
_shotFileKey = k; _shotFileNum = shotNumber;
_shotFileInput.click();
}
async function uploadShotAsset(k, shotNumber, file) {
if (!S.activeWork) { toast("请先创建作品"); return; }
if (file.size > 100 * 1024 * 1024) { toast("文件超过 100MB 上限，已拒绝上传"); return; }
const mt = file.type || "";
if (mt && !mt.startsWith("image/") && !mt.startsWith("video/")) { toast("只支持图片或视频文件"); return; }
toast("正在上传 " + file.name + "…");
try {
const intent = V1(await ApiClient.post("/api/v1/assets/upload-intents", {
org_id: S.org.id, work_id: S.activeWork.id,
filename: file.name, mime: file.type || "application/octet-stream", size: file.size,
}));
const fd = new FormData();
fd.append("upload_token", intent.upload_token);
fd.append("file", file);
const r = await fetch("/api/v1/assets/upload", {
method: "POST", headers: { Authorization: "Bearer " + AuthClient.getAccessToken()},
credentials: "include", body: fd,
});
const d = await r.json().catch(() => ({}));
if (!r.ok) throw new Error(d.detail || d.message || "上传失败");
const assetId = V1(d) ? V1(d).id : null;
if (!assetId) throw new Error("上传成功但未返回素材 id");
const ids = ((getAcc(k) || {}).asset_ids || []).slice();
if (!ids.includes(assetId)) ids.push(assetId);
toast("素材已关联到该镜头");
await loadAssets();
const cur = getAcc(k);
await submitAcceptanceRaw(k, shotNumber, (cur && cur.status) || "pending", (cur && cur.note) || "", ids, null);
} catch (e) {
toast("素材上传失败：" + (e.message || "网络错误"));
}
}

/* 补拍清单：GET /api/v2/shoot/works/{id}/reshoot-list */
function ensureReshootModal() {
if ($("reshoot_modal")) return;
const m = document.createElement("div");
m.className = "modal-mask"; m.id = "reshoot_modal";
m.innerHTML = `<div class="modal" style="max-width:640px"><h3>补拍清单</h3>
<div id="reshoot_body" style="max-height:50vh;overflow:auto"></div>
<div style="display:flex;margin-top:16px"><button class="btn ghost sm" id="btn_close_reshoot" style="margin-left:auto">关闭</button></div></div>`;
document.body.appendChild(m);
m.addEventListener("click", (e) => { if (e.target === m || e.target.id === "btn_close_reshoot") m.classList.remove("show"); });
}
async function showReshootList() {
if (!S.activeWork) { toast("请先创建作品"); return; }
ensureReshootModal();
const body = $("reshoot_body");
body.innerHTML = '<p style="font-size:13.5px;color:var(--muted)">加载中…</p>';
$("reshoot_modal").classList.add("show");
try {
const d = RAW(await ApiClient.get(`/api/v2/shoot/works/${S.activeWork.id}/reshoot-list`)) || {};
const items = d.reshoot || [];
const b = $("btn_reshoot_list");
if (b) b.textContent = `补拍清单（${items.length}）`;
if (!items.length) {
body.innerHTML = '<p style="font-size:14px;color:var(--muted);padding:18px 4px">暂无需要补拍的镜头</p>';
} else {
body.innerHTML = items.map((it) => `
<div style="border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px">
<b style="font-size:13.5px">镜头 ${esc(it.shot_number)} · ${esc(it.shot_purpose || "")}</b>
${it.note ? `<p style="font-size:12.5px;margin:6px 0"><span class="pill red" style="font-size:11.5px">问题</span> ${esc(it.note)}</p>` : ""}
${it.reshoot_guide ? `<p style="font-size:12.5px;margin:6px 0;color:var(--ink2)">补拍指引：${esc(it.reshoot_guide)}</p>` : ""}
${(it.failure_modes || []).length ? `<ul style="font-size:12.5px;color:var(--muted);margin:6px 0;padding-left:18px">${it.failure_modes.map((f) => `<li>${esc(f.symptom || "")} → ${esc(f.fix || "")}</li>`).join("")}</ul>` : ""}
</div>`).join("");
}
} catch (e) {
body.innerHTML = '<p class="err-text">加载失败：' + esc(e.message || "网络错误") + "</p>";
}
}

/* 剪辑清单下载：GET /api/v2/shoot/works/{id}/edit-list.docx */
async function exportEditList() {
if (!S.activeWork) { toast("请先创建作品"); return; }
errText("export_err", "");
toast("正在生成剪辑清单…");
try {
const r = await fetch(`/api/v2/shoot/works/${S.activeWork.id}/edit-list.docx`, {
headers: { Authorization: "Bearer " + AuthClient.getAccessToken()},
credentials: "include",
});
if (!r.ok) {
const d = await r.json().catch(() => ({}));
throw new Error(d.detail || d.message || ("下载失败：" + r.status));
}
const blob = await r.blob();
const name = ((S.data && S.data.product && S.data.product.product_name) || "未命名");
const a = document.createElement("a");
a.href = URL.createObjectURL(blob);
a.download = `剪辑清单_${name}.docx`;
document.body.appendChild(a); a.click(); a.remove();
setTimeout(() => URL.revokeObjectURL(a.href), 5000);
toast("剪辑清单已下载");
} catch (e) {
errText("export_err", e.message || "下载失败");
toast("剪辑清单下载失败：" + (e.message || "网络错误"));
}
}

/* V2.0 P1：只改 app.js，用 JS 注入 UI（不改 HTML） */
function injectP1UI() {
const bp = $("btn_print");
if (bp && !$("btn_reshoot_list")) {
const b = document.createElement("button");
b.className = "btn ghost sm"; b.id = "btn_reshoot_list";
b.textContent = "补拍清单";
b.addEventListener("click", showReshootList);
bp.parentNode.insertBefore(b, bp);
}
const bea = $("btn_export_all");
if (bea && !$("exp_editlist")) {
const row = document.createElement("div");
row.className = "dl-row";
row.innerHTML = `<span class="dl-ic" style="background:#7A3E1F">剪辑</span><div><b style="font-size:13.5px">剪辑交付清单</b><div style="font-size:11.5px;color:var(--muted)">已验收素材的剪辑时间轴</div></div><button class="btn ghost sm" id="exp_editlist">下载</button>`;
bea.parentNode.insertBefore(row, bea);
row.querySelector("#exp_editlist").addEventListener("click", exportEditList);
}
}

/* ================= #7 店播场控脚本 ================= */

async function pollAiTask(taskId, label, timeoutMs) {
  const t0 = Date.now();
  // 对齐后端 ai_tasks.TASK_TIMEOUT_SECONDS=600s，再留 30s 余量读终态；
  // 630s 仍未终态时只报“状态未知”，绝不宣称任务已失败——此前 180s
  // 就报“已自动标记为失败”是错的：前端停轮询时后端可能还在跑。
  timeoutMs = timeoutMs || 630000;
  while (Date.now() - t0 < timeoutMs) {
    const t = RAW(await ApiClient.get(`/api/v1/ai-tasks/${taskId}`));
    if (t.status === "succeeded") return t.result || {};
    if (t.status === "failed" || t.status === "cancelled") {
      throw new Error((label || "AI 任务") + "失败：" + (t.error || "未知错误"));
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  const err = new Error((label || "AI 任务") + "超时（超过10分30秒未返回），任务状态未知、后端可能还在跑，不要重复创建；任务ID：" + taskId + "，刷新页面可继续查询");
  err.taskTimeout = true;
  err.taskId = taskId;
  throw err;
}

/* V3.1 §3.3：单段重生成的纯合并逻辑（无 DOM 依赖，可被 node 测试）。
   返回新数组：仅替换 idx 处的段；newSeg 时间轴必须与原段一致，
   否则抛错（隔离性：其他段落绝不被触碰）。 */
function mergeRegenSegment(segments, idx, newSeg) {
  if (!Array.isArray(segments) || idx < 0 || idx >= segments.length) {
    throw new Error("段索引越界");
  }
  const orig = segments[idx] || {};
  if (newSeg.start_minute !== orig.start_minute ||
      newSeg.end_minute !== orig.end_minute) {
    throw new Error("重生成结果时间轴与原段不一致，已拒绝写入");
  }
  return segments.map((s, i) => (i === idx ? { ...s, ...newSeg } : s));
}
/* V3.1-TEST-MARKER: mergeRegenSegment end */

function renderLivestream() {
  const box = $("live_result");
  const sc = S.data.livestream_script;
  if (!sc || !(sc.segments || []).length) {
    $("live_status").textContent = "未生成";
    $("live_status").className = "pill gray";
    $("btn_export_live").style.display = "none";
    // 必须清空：切换到一个没有脚本的作品时，否则会残留上一个作品的话术
    box.innerHTML = '<p style="font-size:13.5px;color:var(--muted)">选择品类与时长，AI 按"开场留人 → 讲品·互动·逼单循环 → 福袋 → 返场收尾"的真实店播节奏生成全场话术。</p>';
    return;
  }
  $("live_status").textContent = "已生成";
  $("live_status").className = "pill green";
  $("btn_export_live").style.display = "";
  const segs = (sc.segments || []).map((s, i) => `
    <div class="live-seg" data-seg="${i}">
      <span class="live-phase">${esc(s.phase || "")}</span><span class="live-time">${esc(String(s.start_minute ?? ""))}–${esc(String(s.end_minute ?? ""))} 分钟</span>
      ${s.locked ? `<span class="pill amber" style="font-size:11.5px">已锁定</span>` : ""}
      <span class="live-seg-ops" style="margin-left:auto;display:flex;gap:6px">
        <button class="btn ghost sm" data-op="edit" ${s.locked ? "disabled" : ""}>编辑</button>
        <button class="btn ghost sm" data-op="regen" ${s.locked ? "disabled" : ""}>重生成</button>
        <button class="btn ghost sm" data-op="lock">${s.locked ? "解锁" : "锁定"}</button>
        <button class="btn ghost sm" data-op="copy">复制</button>
      </span>
      ${s.goal ? `<div class="live-goal">目标：${esc(s.goal)}</div>` : ""}
      <ul class="live-script">${(s.script || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${(s.tips || []).map((t) => `<div class="live-tip">场控提示：${esc(t)}</div>`).join("")}
      <div class="live-seg-edit" style="display:none"></div>
    </div>`).join("");
  const bags = (sc.lucky_bag_points || []).map((b) =>
    `<li>${esc(String(b.at_minute ?? ""))} 分钟 · ${esc(b.name || "福袋")}：${esc(b.script || "")}</li>`).join("");
  const closing = (sc.closing_scripts || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const rescue = (sc.rescue_scripts || []).map((x) =>
    `<li><b>${esc(x.scene || "")}</b>：${esc(x.script || "")}</li>`).join("");
  const prep = (sc.prep_checklist || []).map((x) => `<li>${esc(x)}</li>`).join("");
  // V3.1 §3.3：待确认清单——占位符信息逐项列出
  const pc = (sc.pending_confirms || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const meta = (sc.prompt_version || sc.generated_at)
    ? `<p style="font-size:11.5px;color:var(--muted);margin-top:8px">脚本版本 ${esc(sc.schema_version || "livestream_script_v1")} · 提示词 ${esc(sc.prompt_version || "—")}${sc.generated_at ? " · 生成于 " + esc(sc.generated_at.slice(0, 16).replace("T", " ")) : ""}</p>`
    : "";
  box.innerHTML = `
    <h4 style="font-size:14px;margin-bottom:4px">${esc(sc.title || "店播场控脚本")}</h4>
    <div class="live-tl">${segs}</div>
    ${bags ? `<div class="live-block"><h4>福袋互动点</h4><ul class="live-script">${bags}</ul></div>` : ""}
    ${closing ? `<div class="live-block"><h4>转款逼单话术</h4><ul class="live-script">${closing}</ul></div>` : ""}
    ${rescue ? `<div class="live-block"><h4>冷场救场话术</h4><ul class="live-script">${rescue}</ul></div>` : ""}
    ${prep ? `<div class="live-block"><h4>开播前准备</h4><ul class="live-script">${prep}</ul></div>` : ""}
    ${pc ? `<div class="live-block"><h4>⚠ 待确认清单</h4><ul class="live-script">${pc}</ul><p style="font-size:12.5px;color:var(--muted)">以上信息脚本中为占位符，开播前请与商家确认后替换</p></div>` : ""}${meta}`;
  box.querySelectorAll(".live-seg").forEach((el) => {
    const i = +el.dataset.seg;
    el.querySelectorAll("[data-op]").forEach((b) => b.addEventListener("click", () => liveSegOp(i, b.dataset.op)));
  });
}

/* V3.1 §3.3：分段操作分发 */
async function liveSegOp(i, op) {
  const sc = S.data.livestream_script;
  const s = sc && (sc.segments || [])[i];
  if (!s) return;
  if (op === "lock") {
    s.locked = !s.locked;
    await saveWork(); renderLivestream();
    toast(s.locked ? "该段已锁定（不再参与重生成）" : "该段已解锁");
    return;
  }
  if (op === "copy") {
    const txt = `【${s.phase || ""}】${s.start_minute ?? ""}–${s.end_minute ?? ""} 分钟\n` +
      (s.script || []).join("\n") + "\n" + (s.tips || []).map((t) => "※ " + t).join("\n");
    try { await navigator.clipboard.writeText(txt); toast("已复制该段"); }
    catch (e) { toast("复制失败：" + (e.message || "")); }
    return;
  }
  if (op === "edit") { liveSegEdit(i); return; }
  if (op === "regen") { regenLivestreamSegment(i); return; }
}

/* V3.1 §3.3：单段编辑（行内改话术/提示，保存才写回） */
function liveSegEdit(i) {
  const sc = S.data.livestream_script;
  const s = sc.segments[i];
  const box = document.querySelector(`.live-seg[data-seg="${i}"] .live-seg-edit`);
  if (!box || box.style.display !== "none") return;
  box.style.display = "";
  box.innerHTML = `
    <div style="margin-top:8px"><div style="font-size:12.5px;color:var(--muted);margin-bottom:4px">主播话术（每行一句）</div>
    <textarea rows="5" style="width:100%" id="live_edit_script_${i}">${esc((s.script || []).join("\n"))}</textarea></div>
    <div style="margin-top:8px"><div style="font-size:12.5px;color:var(--muted);margin-bottom:4px">场控提示（每行一条）</div>
    <textarea rows="3" style="width:100%" id="live_edit_tips_${i}">${esc((s.tips || []).join("\n"))}</textarea></div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn primary sm" id="live_edit_save_${i}">保存本段</button>
      <button class="btn ghost sm" id="live_edit_cancel_${i}">取消</button>
    </div>`;
  $(`live_edit_save_${i}`).addEventListener("click", async () => {
    s.script = $(`live_edit_script_${i}`).value.split("\n").map((x) => x.trim()).filter(Boolean);
    s.tips = $(`live_edit_tips_${i}`).value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!s.script.length) { toast("话术不能为空"); return; }
    sc.updated_at = new Date().toISOString();
    await saveWork(); renderLivestream(); toast("该段已保存");
  });
  $(`live_edit_cancel_${i}`).addEventListener("click", () => renderLivestream());
}

/* V3.1 §3.3：单段重生成——只替换该段，其他段落不受影响 */
async function regenLivestreamSegment(i) {
  const sc = S.data.livestream_script;
  const s = sc && (sc.segments || [])[i];
  if (!s || s.locked) return;
  errText("live_err", "");
  aiLoading("正在重生成该段话术…");
  setBtnsDisabled(["btn_gen_live"], true);
  try {
    const t = RAW(await ApiClient.post("/api/v1/ai-tasks", {
      task_type: "livestream_regen_segment",
      org_id: S.org.id,
      payload: {
        product: S.data.product || {},
        facts: usableFacts(),
        category: $("live_category").value,
        duration_minutes: parseInt($("live_duration").value, 10) || 120,
        extra: liveExtra(),
        segment_index: i,
        segment: { phase: s.phase, start_minute: s.start_minute,
                   end_minute: s.end_minute, goal: s.goal || "" },
      },
    }, { idempotencyKey: "auto" }));
    const result = await pollAiTask(t.id, "该段话术");
    const newSeg = result.segment;
    if (!newSeg) throw new Error("AI 返回的段结构异常，请重试");
    // V3.1 §3.3 隔离性：纯函数合并，时间轴不一致直接拒绝
    sc.segments = mergeRegenSegment(sc.segments, i, newSeg);
    sc.updated_at = new Date().toISOString();
    await saveWork();
    renderLivestream();
    toast("该段已重生成，其他段落未动");
  } catch (e) {
    errText("live_err", e.message || "重生成失败");
  } finally {
    setBtnsDisabled(["btn_gen_live"], false);
    aiLoadingDone();
  }
}

/* V3.1：读取直播可选输入 */
function liveExtra() {
  const v = (id) => { const el = $(id); return el ? el.value.trim() : ""; };
  return { platform: v("live_platform"), goal: v("live_goal"),
           style: v("live_style"), promo: v("live_promo") };
}

/* V3.1 P1-4：task_id 持久化（sessionStorage，刷新页面可恢复轮询） */
function liveTaskKey() {
  return `lupin_live_task_${S.org ? S.org.id : "-"}_${S.activeWork ? S.activeWork.id : "-"}`;
}
function saveLiveTaskId(taskId) {
  try { sessionStorage.setItem(liveTaskKey(), taskId); } catch (e) { /* 忽略 */ }
}
function clearLiveTaskId() {
  try { sessionStorage.removeItem(liveTaskKey()); } catch (e) { /* 忽略 */ }
}
async function resumeLivestreamTask() {
  let taskId = null;
  try { taskId = sessionStorage.getItem(liveTaskKey()); } catch (e) { /* 忽略 */ }
  if (!taskId || !S.org || !S.activeWork) return;
  // 已有生成结果则无需恢复
  if ((S.data.livestream_script || {}).segments) { clearLiveTaskId(); return; }
  errText("live_err", "");
  aiLoading("正在恢复未完成的场控脚本任务…");
  let taskTimeout = false;
  try {
    const result = await pollAiTask(taskId, "场控脚本");
    if (result.livestream_script && (result.livestream_script.segments || []).length) {
      S.data.livestream_script = result.livestream_script;
      await saveWork();
      toast("已恢复之前生成的场控脚本");
    }
  } catch (e) {
    // 任务失败/不存在：清掉记录；轮询超时只是状态未知，保留任务ID
    // 供下次刷新继续查询，绝不能清掉后让用户误以为任务已失败。
    taskTimeout = !!(e && e.taskTimeout);
    errText("live_err", e.message || "");
  } finally {
    if (!taskTimeout) clearLiveTaskId();
    renderLivestream();
    aiLoadingDone();
  }
}

async function genLivestreamScript() {
  const category = $("live_category").value;
  const duration = parseInt($("live_duration").value, 10) || 120;
  errText("live_err", "");
  aiLoading("正在生成场控脚本…");
  setBtnsDisabled(["btn_gen_live"], true); // V3.1 P0：防双击重复扣费
  try {
    const t = RAW(await ApiClient.post("/api/v1/ai-tasks", {
      task_type: "livestream_script",
      org_id: S.org.id,
      payload: {
        product: S.data.product || {},
        facts: usableFacts(),
        category: category,
        duration_minutes: duration,
        extra: liveExtra(), // V3.1：直播平台/目标/风格/促销（可选）
      },
    }, { idempotencyKey: "auto" })); // V3.1 P0：同 key 只建一个任务
    saveLiveTaskId(t.id); // V3.1 P1-4：刷新页面可恢复轮询
    const result = await pollAiTask(t.id, "场控脚本");
    if (!result.livestream_script || !(result.livestream_script.segments || []).length) {
      throw new Error("AI 返回的脚本结构异常，请重试");
    }
    S.data.livestream_script = result.livestream_script;
    await saveWork();
    clearLiveTaskId(); // 成功：任务已终态，无需恢复
    renderLivestream();
    toast("场控脚本已生成");
  } catch (e) {
    // V3.1 P1-4：前端超时 ≠ 后端失败——超时时保留 task_id，
    // 用户刷新页面仍可恢复轮询；明确失败/取消才清除
    if (!/超时/.test(e.message || "")) clearLiveTaskId();
    errText("live_err", e.message || "生成失败");
  } finally {
    setBtnsDisabled(["btn_gen_live"], false);
    aiLoadingDone();
  }
}

function exportLivestreamTxt() {
  const sc = S.data.livestream_script;
  if (!sc) { toast("请先生成场控脚本"); return; }
  const p = S.data.product || {};
  let txt = `${sc.title || "店播场控脚本"}\n产品：${p.product_name || ""} · 总时长约 ${sc.total_minutes || ""} 分钟\n\n`;
  (sc.segments || []).forEach((s) => {
    txt += `\n【${s.phase || ""}】${s.start_minute ?? ""}–${s.end_minute ?? ""} 分钟${s.goal ? " · 目标：" + s.goal : ""}\n`;
    (s.script || []).forEach((x) => { txt += `  ${x}\n`; });
    (s.tips || []).forEach((t) => { txt += `  ※ 场控提示：${t}\n`; });
  });
  if ((sc.lucky_bag_points || []).length) {
    txt += "\n【福袋互动点】\n";
    sc.lucky_bag_points.forEach((b) => { txt += `  ${b.at_minute} 分钟 · ${b.name}：${b.script}\n`; });
  }
  if ((sc.closing_scripts || []).length) {
    txt += "\n【转款逼单话术】\n";
    sc.closing_scripts.forEach((x) => { txt += `  ${x}\n`; });
  }
  if ((sc.rescue_scripts || []).length) {
    txt += "\n【冷场救场话术】\n";
    sc.rescue_scripts.forEach((x) => { txt += `  ${x.scene}：${x.script}\n`; });
  }
  // V3.1 §3.3：待确认清单
  if ((sc.pending_confirms || []).length) {
    txt += "\n【待确认清单】\n";
    sc.pending_confirms.forEach((x) => { txt += `  ⚠ ${x}\n`; });
    txt += "（以上信息脚本中为占位符，开播前请与商家确认后替换）\n";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([txt], { type: "text/plain;charset=utf-8" }));
  a.download = "店播场控脚本.txt";
  document.body.appendChild(a); a.click(); a.remove();
  toast("已导出 TXT");
}

/* ================= #9 拉呱版改写器 ================= */

let colloqShotIdx = -1;
let colloqOrigHash = ""; // V3.1 §4.2：打开 modal 时的原文哈希，用于并发编辑检测

function colloqHash(t) {
  let h = 0;
  const s = String(t || "");
  for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
  return String(h);
}

async function openColloquialModal(i) {
  // V2.1 #3：改写请求进行中不再重复触发（防双击重复扣费）
  if (openColloquialModal._busy) return;
  const shots = (S.data.storyboard || {}).shots || [];
  const s = shots[i];
  if (!s) return;
  const orig = String(s.voiceover || s.subtitle || "").trim();
  if (!orig) { toast("该镜头暂无口播文案"); return; }
  openColloquialModal._busy = true;
  colloqShotIdx = i;
  colloqOrigHash = colloqHash(orig); // V3.1 §4.2：记录原文快照哈希
  $("colloq_orig").textContent = orig;
  $("colloq_new").textContent = "改写中…";
  $("colloq_changes").innerHTML = "";
  $("colloq_meta").textContent = "";
  $("colloq_conflict").style.display = "none";
  errText("colloq_err", "");
  updateColloqUndoBtn();
  $("colloquial_modal").classList.add("show");
  try {
    const d = RAW(await ApiClient.post("/api/colloquial-rewrite", {
      text: orig,
      product_name: (S.data.product || {}).product_name || "",
      work_id: (S.activeWork || {}).id || null, // V3.1 §4.1：服务端取已确认事实
      product_id: S.selectedProductId || null,
    }, { idempotencyKey: "auto" }));
    $("colloq_new").textContent = d.rewritten || "—";
    $("colloq_changes").innerHTML =
      (d.changes || []).map((c) => `<li>${esc(c)}</li>`).join("");
    // V3.1 §4.1：展示事实 grounding 情况（可信度可见）
    const fv = d.prompt_version ? `提示词 ${esc(d.prompt_version)}` : "";
    const fc = (d.facts_used != null) ? ` · 已注入 ${d.facts_used} 条已确认事实` : "";
    $("colloq_meta").textContent = (fv + fc).replace(/^ · /, "");
  } catch (e) {
    // V3.1 §4 P1-5：改写失败 → 原文不受影响（只清空改写区，不碰镜头数据）
    $("colloq_new").textContent = "—";
    errText("colloq_err", e.message || "改写失败");
  } finally {
    openColloquialModal._busy = false;
  }
}

async function useColloquial() {
  const shots = (S.data.storyboard || {}).shots || [];
  const s = shots[colloqShotIdx];
  const nw = $("colloq_new").textContent.trim();
  if (!s || !nw || nw === "改写中…" || nw === "—") return;
  // V3.1 §4.2：并发编辑检测——改写期间目标镜头被改过则弹冲突面板，不直接覆盖
  const cur = String(s.voiceover || s.subtitle || "").trim();
  if (colloqHash(cur) !== colloqOrigHash) {
    $("colloq_cur").textContent = cur;
    $("colloq_new2").textContent = nw;
    $("colloq_conflict").style.display = "";
    return;
  }
  await doUseColloquial(false);
}

async function doUseColloquial(force) {
  const shots = (S.data.storyboard || {}).shots || [];
  const s = shots[colloqShotIdx];
  const nw = (force ? $("colloq_new2") : $("colloq_new")).textContent.trim();
  if (!s || !nw || nw === "改写中…" || nw === "—") return;
  // V3.1 §4.2：原文快照入撤销栈（持久化到作品，可恢复）
  if (!Array.isArray(S.data.colloq_undo)) S.data.colloq_undo = [];
  S.data.colloq_undo.push({
    shot_idx: colloqShotIdx,
    prev: s.voiceover || "",
    next: nw,
    at: new Date().toISOString(),
  });
  if (S.data.colloq_undo.length > 20) S.data.colloq_undo.shift(); // 只保留最近 20 条
  if (nw !== (s.voiceover || "")) { markRiskDirty(); } // V2.1：口播文案变更 → 风险检查标脏
  s.voiceover = nw;
  await saveWork();
  renderShots();
  updateRiskDirtyBadge();
  $("colloq_conflict").style.display = "none";
  $("colloquial_modal").classList.remove("show");
  toast(force ? "已覆盖采用（原修改被替换）" : "已采用口语版，可撤销");
}

/* V3.1 §4.2：撤销上次拉呱版采用，恢复原文快照 */
async function undoColloquial() {
  const stack = S.data.colloq_undo || [];
  const last = stack.pop();
  if (!last) { toast("没有可撤销的改写"); return; }
  const shots = (S.data.storyboard || {}).shots || [];
  const s = shots[last.shot_idx];
  if (s) s.voiceover = last.prev;
  markRiskDirty(); // 恢复原文同样使旧风险结论过期
  await saveWork();
  renderShots();
  updateRiskDirtyBadge();
  updateColloqUndoBtn();
  toast("已恢复原文");
}

function updateColloqUndoBtn() {
  const el = $("btn_undo_colloquial");
  if (el) el.style.display = ((S.data.colloq_undo || []).length ? "" : "none");
}
function fillNote() { $("shoot_note").value = (S.data && S.data.shoot_note) || "";}

/* ---------- 素材（上传意图流程） ---------- */
async function loadAssets() {
if (!S.org ||!S.activeWork) return;
const grid = $("asset_grid");
try {
const list = V1(await ApiClient.get(`/api/v1/assets?org_id=${encodeURIComponent(S.org.id)}&work_id=${encodeURIComponent(S.activeWork.id)}`)) || [];
$("asset_count").textContent = list.length? `${list.length} 条`: "";
if (!list.length) {
grid.innerHTML = '<div style="grid-column:1/-1">' + emptyState("暂无素材", "点下方按钮，上传图片或视频素材。", true, { label: "上传素材", fn: () => { const f = $("file_asset"); if (f) f.click(); } }, FRAME_SVG) + '</div>';
return;
}
grid.innerHTML = list.map((a) => {
const isImg = (a.mime || "").startsWith("image/");
return `<div class="mat" data-aid="${esc(a.id)}" data-img="${isImg? 1: 0}" title="${esc(a.filename)}"
style="background:linear-gradient(135deg,#5B6B7A,#A8B8C8);display:grid;place-items:center;color:#fff;font-size:11.5px;overflow:hidden">
${isImg? `<img alt="" style="width:100%;height:100%;object-fit:cover">`: `<span style="padding:6px">${esc((a.filename || "").slice(0, 12))}</span>`}
<span class="st pill gray">${esc((a.mime || "").split("/")[0] || "文件")}</span></div>`;
}).join("");
const token = AuthClient.getAccessToken();
grid.querySelectorAll(".mat[data-img='1']").forEach((m) => {
fetch(`/api/v1/assets/${m.dataset.aid}/raw`, { headers: { Authorization: "Bearer " + token}})
.then((r) => (r.ok? r.blob(): null))
.then((b) => { if (b) m.querySelector("img").src = URL.createObjectURL(b);})
.catch(() => {});
});
} catch (e) {
grid.innerHTML = `<p style="font-size:12.5px;color:var(--red);grid-column:1/-1">加载失败：${esc(e.message || "网络错误")}</p>`;
}
}

async function uploadAsset(file) {
errText("asset_err", "");
toast("正在上传 " + file.name + "…");
try {
const intent = V1(await ApiClient.post("/api/v1/assets/upload-intents", {
org_id: S.org.id, work_id: S.activeWork.id,
filename: file.name, mime: file.type || "application/octet-stream", size: file.size,
}));
const fd = new FormData();
fd.append("upload_token", intent.upload_token);
fd.append("file", file);
const r = await fetch("/api/v1/assets/upload", {
method: "POST", headers: { Authorization: "Bearer " + AuthClient.getAccessToken()},
credentials: "include", body: fd,
});
const d = await r.json().catch(() => ({}));
if (!r.ok) throw new Error(d.detail || d.message || "上传失败");
toast("上传成功");
await loadAssets();
} catch (e) {
errText("asset_err", e.message || "上传失败");
}
}

/* ================= v6 交付中心 ================= */
function renderDeliveryTitles() {
const name = (S.data && S.data.product && S.data.product.product_name) || "交付中心";
$("w6_title").textContent = name;
}

async function runDeliveryCheck() {
const sb = S.data.storyboard;
if (!sb) { toast("请先生成分镜"); return;}
errText("delivery_err", "");
aiLoading("正在检查交付状态…");
try {
const d = RAW(await ApiClient.post("/api/delivery-check", {
storyboard: sb, product: S.data.product || {}, facts: usableFacts(),
feasibility: S.data.feasibility || null, shoot_state: S.data.shoot_state || {},
}));
const labels = { done: "已完成", action: "需要处理", suggest: "建议完善", na: "不适用"};
const pillTone = { done: "green", action: "red", suggest: "amber", na: "gray"};
$("w6_status").textContent = d.ready? "可以交付": "尚不能交付";
$("w6_status").className = "pill " + (d.ready? "green": "amber");
$("delivery_results").innerHTML =
`<p style="font-size:14px;font-weight:700;margin-bottom:12px">${d.ready? "✓ 可以交付": "⚠ 尚不能交付"}</p>` +
(d.items || []).map((it, i) => `
<div class="check" data-di="${i}" tabindex="0" role="button" aria-label="确认 ${esc(it.label)}"><span class="box"></span>
<span class="t"><b>${esc(it.label)}</b><br><span style="color:var(--muted);font-size:12.5px">${esc(it.reason || "")}</span></span>
<span class="note pill ${pillTone[it.status] || "gray"}">${esc(labels[it.status] || it.status)}</span></div>`).join("") +
`<p style="font-size:12.5px;color:var(--muted);margin-top:10px">勾选 = 人工已确认。风险提示仅为依据缺失提醒，本工具不保证广告合规，发布前请自行把关。</p>`;
$("delivery_results").querySelectorAll(".check").forEach((c) => { keyActivate(c); c.addEventListener("click", () => c.classList.toggle("done")); });
} catch (e) {
errText("delivery_err", e.message || "检查失败");
} finally {
aiLoadingDone();
}
}

async function loadTimeline() {
const panel = $("timeline_panel");
if (!S.activeWork) { panel.innerHTML = '<p style="font-size:13.5px;color:var(--muted)">请先创建作品。</p>'; return;}
try {
const t = V1(await ApiClient.get(`/api/v1/works/${S.activeWork.id}/timeline`));
if (!t ||!(t.shots || []).length) {
panel.innerHTML = emptyState("暂无分镜时间轴", "先去创意策划生成分镜。", true, { label: "去创意策划", fn: () => go("v3") });
return;
}
panel.innerHTML =
`<p style="font-size:13.5px;margin-bottom:8px">目标 ${t.target_duration_seconds}s · 实际 ${Math.round(t.total_duration_seconds)}s · ` +
`<span class="pill ${t.within_tolerance? "green": "amber"}">${t.within_tolerance? "时长达标": (t.diff_seconds > 0? `超时 ${Math.round(t.diff_seconds)}s`: `不足 ${Math.round(-t.diff_seconds)}s`)}</span></p>` +
(() => { let c = 0;
return t.shots.map((s) => {
const d = parseFloat(s.duration_seconds) || 0;
const st = c; c += d;
return `<div class="tl-item"><span class="tl-time">${Math.round(st)}s – ${Math.round(c)}s</span><span>镜头 ${s.shot_no} · ${Math.round(d)}s</span></div>`;
}).join(""); })();
} catch (e) {
panel.innerHTML = `<p style="font-size:13.5px;color:var(--muted)">时间轴加载失败：${esc(e.message || "")}</p>`;
}
}

/* ---------- 导出 ---------- */
async function downloadBlob(path, body, filename) {
const headers = { "Content-Type": "application/json"};
const t = AuthClient.getAccessToken();
if (t) headers["Authorization"] = "Bearer " + t;
const r = await fetch(path, { method: "POST", headers, credentials: "include", body: JSON.stringify(body)});
if (!r.ok) {
const d = await r.json().catch(() => ({}));
throw new Error(d.detail || d.message || "导出失败");
}
const blob = await r.blob();
const a = document.createElement("a");
a.href = URL.createObjectURL(blob);
a.download = filename;
document.body.appendChild(a); a.click(); a.remove();
setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
function needStoryboard() {
if (!S.data.storyboard) { errText("export_err", "请先生成分镜"); return false;}
errText("export_err", "");
return true;
}
async function exportDocx() {
if (!needStoryboard()) return;
try { await downloadBlob("/api/export", { storyboard: S.data.storyboard}, "拍摄执行单.docx"); toast("已导出 DOCX");}
catch (e) { errText("export_err", e.message);}
}
async function exportSrt() {
if (!needStoryboard()) return;
try { await downloadBlob("/api/export-srt", { storyboard: S.data.storyboard}, "口播字幕.srt"); toast("已导出 SRT");}
catch (e) { errText("export_err", e.message);}
}
function exportTxt() {
const sb = S.data.storyboard;
if (!sb) { errText("export_err", "请先生成分镜"); return;}
errText("export_err", "");
const p = S.data.product || {};
const poster = S.data.poster || {};
const txt = `发布文案\n\n${poster.copy || ""}\n\n—— 口播全文 ——\n` +
(sb.shots || []).map((s) => `镜头${s.shot_number}：${s.voiceover || s.subtitle || ""}`).join("\n");
const a = document.createElement("a");
a.href = URL.createObjectURL(new Blob([txt], { type: "text/plain;charset=utf-8"}));
a.download = "发布文案.txt";
document.body.appendChild(a); a.click(); a.remove();
toast("已导出 TXT");
}

/* ================= 模型配置 ================= */
function openCfg(triggerEl) {
const cfg = getApiConfig();
// 模型配置改走站内通用弹窗 _openUiModal（与 modalConfirm/modalInput 同一容器规范：
// role="dialog" + aria-modal 由 _openUiModal 提供；Esc 关闭、点遮罩关闭、_trapTab 焦点陷阱、
// 关闭后焦点返回由本函数按 modalConfirm 的模式接线，_openUiModal 本身不提供这些行为）。
// 静态 #cfg_modal 已从 app.html 删除（grep 验证 app.html 无 cfg_ 残留），表单内容原样搬进动态 HTML；
// 值用 JS 赋值，不拼进 HTML 字符串；所有查询均用 m.querySelector 相对查询，不用全局 ID 查询。
// 焦点返回：触发元素由点击处理器显式传入（e.currentTarget），不依赖 document.activeElement，
// 避免 Safari 点击不聚焦、程序化调用等场景下 activeElement 不是触发按钮的问题；未传时回退 activeElement。
const trigger = (triggerEl && typeof triggerEl.focus === "function") ? triggerEl : document.activeElement;
const m = _openUiModal(`
  <h3>模型配置</h3>
  <div class="page-meander" style="margin:10px 0 2px" aria-hidden="true"></div>
  <p style="font-size:12.5px;color:var(--muted);margin-bottom:14px">自带 Key 调用 AI（走兼容接口）。Key 只保存在当前浏览器标签页，关闭即清除，不会持久化到本站服务器；但每次调用时都会先发给本站后端、再由后端转发到你填写的 Base URL，请只填可信域名（仅支持 deepkey.top、api.openai.com 及其子域名）。</p>
  <div class="form-stack">
    <label class="f">API Base URL<input id="cfg_base" placeholder="留空用服务端默认"></label>
    <label class="f">API Key<input id="cfg_key" type="password" placeholder="sk-..." autocomplete="off"></label>
    <label class="f">模型名称<input id="cfg_model" placeholder="留空用服务端默认"></label>
  </div>
  <div style="display:flex;gap:10px;margin-top:16px"><button class="btn primary sm" id="btn_save_cfg">保存到当前会话</button><button class="btn ghost sm" id="btn_clear_cfg">清除</button><button class="btn ghost sm" id="btn_close_cfg" style="margin-left:auto">关闭</button></div>
  <p id="cfg_status" style="font-size:12.5px;color:var(--muted);margin-top:12px"></p>`, "模型配置");
m._returnFocus = trigger; // 显式覆盖：关闭时 _closeUiModal 用此元素恢复焦点，不依赖调用瞬间 activeElement
let settled = false;
const done = () => { if (settled) return; settled = true; document.removeEventListener("keydown", onKey, true); _closeUiModal(m); };
m._settle = done;
const onKey = (e) => {
  if (e.key === "Escape") { e.stopPropagation(); done(); }
  else if (e.key === "Tab") _trapTab(m, e);
};
m.addEventListener("click", (e) => { if (e.target === m) done(); });
m.querySelector("#btn_close_cfg").addEventListener("click", done);
m.querySelector("#btn_save_cfg").addEventListener("click", () => saveCfg(m));
m.querySelector("#btn_clear_cfg").addEventListener("click", () => {
  try { sessionStorage.removeItem("lupin_api_base"); sessionStorage.removeItem("lupin_api_key"); sessionStorage.removeItem("lupin_api_model"); } catch (e) {}
  m.querySelector("#cfg_base").value = ""; m.querySelector("#cfg_key").value = ""; m.querySelector("#cfg_model").value = "";
  updateCfgStatus(m); toast("已清除");
});
document.addEventListener("keydown", onKey, true);
const baseEl = m.querySelector("#cfg_base");
baseEl.value = cfg.base;
m.querySelector("#cfg_key").value = "";
m.querySelector("#cfg_model").value = cfg.model;
updateCfgStatus(m);
if (baseEl) baseEl.focus();
}
function updateCfgStatus(modal) {
const cfg = getApiConfig();
const el = modal ? modal.querySelector("#cfg_status") : null;
if (!el) return;
el.textContent = cfg.key? "✓ 已配置自定义 Key（仅当前标签页有效，关闭即清除）": "当前使用服务端默认配置";
}
/* S3 修复（保存侧）：非白名单域名拒绝保存并明示原因。白名单定义与
   _apiHostAllowed 见文件前部 getApiConfig 处（发送侧共用同一函数）。
   拒绝保存时不清除旧配置（旧合法配置继续可用），恶意旧值由发送侧
   getApiConfig 拦截并清除，两层各司其职。 */
function saveCfg(modal) {
try {
const m = modal || document.getElementById("ui_modal");
if (!m) { toast("模型配置弹窗未打开"); return; }
const base = m.querySelector("#cfg_base").value.trim(), key = m.querySelector("#cfg_key").value.trim(), model = m.querySelector("#cfg_model").value.trim();
if (base &&!/^https?:\/\//i.test(base)) { toast("Base URL 必须以 http(s):// 开头"); return;}
if (base && !_apiHostAllowed(base)) { toast("该 Base URL 域名不在可信列表内（仅支持 deepkey.top、api.openai.com 及其子域名），Key 不会被保存，以免 Key 被发送到不可信域名"); return;}
if (base) sessionStorage.setItem("lupin_api_base", base); else sessionStorage.removeItem("lupin_api_base");
if (key) sessionStorage.setItem("lupin_api_key", key); else sessionStorage.removeItem("lupin_api_key");
if (model) sessionStorage.setItem("lupin_api_model", model); else sessionStorage.removeItem("lupin_api_model");
m.querySelector("#cfg_key").value = "";
updateCfgStatus(m);
toast("已保存到当前会话");
} catch (e) { toast("保存失败：浏览器存储不可用");}
}

/* ================= 动效（沿用设计稿） ================= */
const io = new IntersectionObserver((es) => es.forEach((e) => {
if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target);}
}), { threshold: 0.1});
function revealIn(root) {
if (!root) return;
root.querySelectorAll(".card,.panel,.greet,.stepper,.marquee").forEach((el) => {
if (!el.classList.contains("in")) { el.classList.add("reveal"); io.observe(el);}
});
}

/* ================= 待接入 5 项补齐 ================= */

/* multipart 上传 helper（ApiClient 只做 JSON） */
async function postForm(path, formData) {
  const headers = { "X-Request-ID": ApiClient.genRequestId() };
  try {
    const t = AuthClient.getAccessToken();
    if (t) headers["Authorization"] = "Bearer " + t;
  } catch (e) { /* 忽略 */ }
  const r = await fetch(path, { method: "POST", headers, credentials: "include", body: formData });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error((data && (data.message || data.detail)) || ("请求失败 HTTP " + r.status));
    err.status = r.status; err.code = data.code; throw err;
  }
  return data;
}

/* ---------- #1 产品文档导入（v2） ---------- */
let IMPORT_CANDS = [];
async function onImportDocFile(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  if (!/\.(txt|md)$/i.test(f.name)) { toast("只支持 .txt / .md 文件"); return; }
  if (f.size > 2 * 1024 * 1024) { toast("文件过大（上限 2MB）"); return; }
  if (!S.activeWork) { toast("请先创建作品"); return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("org_id", S.org.id);
  fd.append("product_id", ((S.data.product || {}).product_name) || "");
  errText("product_err", "");
  toast("正在解析文档…");
  try {
    const d = V1(await postForm("/api/v1/facts/import-document", fd));
    IMPORT_CANDS = d.candidates || [];
    if (!IMPORT_CANDS.length) { toast("文档中未抽取出事实候选"); return; }
    $("import_filename").textContent = d.filename || f.name;
    const badge = $("import_ai_badge");
    badge.textContent = d.ai_used ? "AI 抽取" : "规则解析";
    badge.className = "pill " + (d.ai_used ? "green" : "gray");
    $("import_candidates").innerHTML = IMPORT_CANDS.map((c, i) =>
      '<label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;align-items:flex-start">' +
      '<input type="checkbox" data-i="' + i + '" checked style="width:auto;margin-top:4px">' +
      '<span style="flex:1"><span style="font-size:13.5px">' + esc(c.content) + '</span><br>' +
      '<span class="pill gray" style="margin-top:4px;display:inline-block">' + esc(c.category) + '</span>' +
      '<span style="font-size:11.5px;color:var(--muted);margin-left:6px">' + (c.source === "ai" ? "AI" : "规则") + '</span></span></label>'
    ).join("");
    $("import_modal").classList.add("show");
  } catch (err) { errText("product_err", err.message || "导入失败"); }
}
async function confirmImport() {
  const checked = Array.from(document.querySelectorAll("#import_candidates input:checked"))
    .map((x) => IMPORT_CANDS[+x.dataset.i]).filter(Boolean);
  if (!checked.length) { $("import_msg").textContent = "请至少勾选一条"; return; }
  $("import_msg").textContent = "";
  try {
    const d = V1(await ApiClient.post("/api/v1/facts/batch", {
      org_id: S.org.id,
      product_id: ((S.data.product || {}).product_name) || "",
      facts: checked.map((c) => ({ content: c.content, category: c.category,
        source_type: "文档导入", source_ref: $("import_filename").textContent })),
    }));
    $("import_modal").classList.remove("show");
    await loadFacts();
    toast("已导入 " + d.length + " 条事实（草稿）");
  } catch (err) { $("import_msg").textContent = err.message || "导入失败"; }
}

/* ---------- #2 成片上传归档（v6） ---------- */
async function uploadFinalVideo(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  if (!S.activeWork) { toast("请先创建作品"); return; }
  if (f.size > 200 * 1024 * 1024) { toast("文件过大（上限 200MB）"); return; }
  errText("export_err", "");
  toast("正在上传成片…");
  try {
    const intent = V1(await ApiClient.post("/api/v1/assets/upload-intents", {
      org_id: S.org.id, work_id: S.activeWork.id,
      filename: f.name, mime: f.type || "video/mp4", size: f.size,
    }));
    const fd = new FormData();
    fd.append("upload_token", intent.upload_token);
    fd.append("file", f);
    await postForm("/api/v1/assets/upload", fd);
    toast("成片已归档");
    await renderFinalArchive();
  } catch (err) { errText("export_err", err.message || "上传失败"); }
}
async function _finalDownloadUrl(aid) {
  const d = V1(await ApiClient.get("/api/v1/assets/" + aid + "/download?org_id=" + encodeURIComponent(S.org.id)));
  return d.url;
}
async function toggleFinalPreview(aid, row) {
  const box = row.nextElementSibling;
  if (box.style.display !== "none") { box.style.display = "none"; box.innerHTML = ""; return; }
  try {
    const url = await _finalDownloadUrl(aid);
    box.innerHTML = '<video controls preload="metadata" style="width:100%;border-radius:10px;background:#000" src="' + esc(url) + '"></video>';
    box.style.display = "";
  } catch (err) { toast(err.message || "预览失败"); }
}
async function downloadFinal(aid) {
  try {
    const url = await _finalDownloadUrl(aid);
    const a = document.createElement("a");
    a.href = url; a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  } catch (err) { toast(err.message || "下载失败"); }
}
async function deleteFinal(aid) {
  if (!(await modalConfirm({ title: "删除归档成片", body: "确定删除这条归档成片吗？\n此操作不可恢复。", okText: "删除", danger: true }))) return;
  try {
    await ApiClient.del("/api/v1/assets/" + aid + "?org_id=" + encodeURIComponent(S.org.id));
    toast("已删除");
    await renderFinalArchive();
  } catch (err) { toast(err.message || "删除失败"); }
}
async function renderFinalArchive() {
  const box = $("final_archive_list");
  if (!S.activeWork) { box.innerHTML = ""; return; }
  try {
    const list = V1(await ApiClient.get("/api/v1/assets?org_id=" + encodeURIComponent(S.org.id) +
      "&work_id=" + encodeURIComponent(S.activeWork.id))) || [];
    const videos = list.filter((a) => (a.mime || "").indexOf("video/") === 0);
    if (!videos.length) {
      box.innerHTML = emptyState("暂无归档成片", "拍好的成片可以归档到这里。", true, { label: "上传成片", fn: () => { const b = $("btn_upload_final"); if (b) b.click(); } });
      return;
    }
    box.innerHTML = videos.map((v) =>
      '<div class="dl-row" data-aid="' + esc(v.id) + '">' +
      '<span class="dl-ic" style="background:#0F5132">MP4</span>' +
      '<div style="flex:1;min-width:0"><b style="font-size:13.5px">' + esc(v.filename) + '</b>' +
      '<div style="font-size:11.5px;color:var(--muted);white-space:nowrap">' + (v.size / 1048576).toFixed(1) + ' MB · ' + esc((v.created_at || "").slice(0, 10)) + '</div></div>' +
      '<button class="btn ghost sm" data-act="preview">预览</button>' +
      '<button class="btn ghost sm" data-act="dl">下载</button>' +
      '<button class="btn ghost sm" data-act="del">删除</button></div>' +
      '<div style="display:none;padding:0 6px 10px"></div>'
    ).join("");
    box.querySelectorAll(".dl-row").forEach((row) => {
      const aid = row.dataset.aid;
      row.querySelector('[data-act="preview"]').addEventListener("click", () => toggleFinalPreview(aid, row));
      row.querySelector('[data-act="dl"]').addEventListener("click", () => downloadFinal(aid));
      row.querySelector('[data-act="del"]').addEventListener("click", () => deleteFinal(aid));
    });
  } catch (err) {
    box.innerHTML = '<p class="err-text">加载失败：' + esc(err.message || "") + '</p>';
  }
}

/* ---------- #3 示例案例库（v1） ---------- */
const SAMPLE_CASES = [
  { id: "apple", name: "烟台苹果",
    img: "/static/assets/landing/products/yantai-apples.jpg",
    desc: "果园采摘 → 枝头特写 → 切开展示，三个镜头讲清新鲜，适合当季上新时拍摄。",
    product: { product_name: "烟台苹果", category: "水果 / 苹果", origin: "山东 · 烟台",
      spec: "5斤装", price: "5斤装 29.9元", channel: "线上零售",
      points: ["脆甜多汁", "果园现摘现发", "果径 80mm 以上"], duration: 60 },
    facts: [
      { content: "烟台苹果产自山东烟台", category: "产地" },
      { content: "果径 80mm 以上", category: "规格" },
      { content: "脆甜多汁，果园现摘现发", category: "口感" },
      { content: "5斤装礼盒装，29.9元", category: "规格" },
    ],
    idea: { name: "果园现摘的新鲜感", tagline: "从枝头到舌尖，全程看得见",
      hook_3s: "刚摘下来的苹果，咬一口是什么声音？",
      core_idea: "用采摘→特写→切开三组镜头建立新鲜信任，口播强调现摘现发、当季上新",
      audience_fit: "注重品质的家庭水果消费者", difficulty: "简单",
      materials_needed: "手机、三脚架、水果刀、果篮", fact_refs: [] } },
  { id: "scallion", name: "章丘大葱",
    img: "/static/assets/landing/products/zhangqiu-scallions.jpg",
    desc: "田间实拍加对比镜头，突出葱白长、葱株挺拔的视觉特点，一镜到底也好拍。",
    product: { product_name: "章丘大葱", category: "蔬菜 / 大葱", origin: "山东 · 济南章丘",
      spec: "10斤装", price: "10斤装 19.9元", channel: "线上零售",
      points: ["葱白长达60厘米", "葱株挺拔", "田间现挖现发"], duration: 60 },
    facts: [
      { content: "章丘大葱产自山东济南章丘", category: "产地" },
      { content: "葱白长达60厘米", category: "规格" },
      { content: "葱株挺拔，口感脆嫩", category: "口感" },
      { content: "田间现挖现发", category: "工艺" },
    ],
    idea: { name: "一镜到底的田间气势", tagline: "比人还高的葱，镜头里见分晓",
      hook_3s: "你见过比人还高的大葱吗？",
      core_idea: "田间实拍 + 对比镜头（与普通大葱/人物对比），一镜到底展示葱白长度",
      audience_fit: "喜欢看猎奇与产地实拍的观众", difficulty: "简单",
      materials_needed: "手机、稳定器", fact_refs: [] } },
  { id: "chicken", name: "德州扒鸡",
    img: "/static/assets/landing/products/dezhou-braised-chicken.jpg",
    desc: "摆盘特写加撕开展示，用热气与脱骨镜头传递口感联想，配文案讲清老字号做法。",
    product: { product_name: "德州扒鸡", category: "熟食 / 扒鸡", origin: "山东 · 德州",
      spec: "500g/只", price: "500g 39.9元", channel: "线上零售",
      points: ["传统卤制工艺", "脱骨酥烂", "老字号做法"], duration: 60 },
    facts: [
      { content: "德州扒鸡源自山东德州", category: "产地" },
      { content: "采用传统卤制工艺", category: "工艺" },
      { content: "脱骨酥烂，肉质细嫩", category: "口感" },
      { content: "500g每只，39.9元", category: "规格" },
    ],
    idea: { name: "热气腾腾的脱骨瞬间", tagline: "一撕脱骨，老字号的底气",
      hook_3s: "轻轻一撕，骨肉分离——这才是扒鸡！",
      core_idea: "摆盘特写开场，撕开镜头 + 热气慢拍传递口感，口播讲老字号卤制工艺",
      audience_fit: "熟食卤味爱好者、送礼需求人群", difficulty: "简单",
      materials_needed: "手机、三脚架、白色摆盘", fact_refs: [] } },
];
function renderCasesGrid() {
  $("cases_grid").innerHTML = SAMPLE_CASES.map((c, i) =>
    '<div class="card" style="padding:0;overflow:hidden">' +
    '<img src="' + c.img + '" alt="' + esc(c.name) + '" style="width:100%;aspect-ratio:16/10;object-fit:cover">' +
    '<div style="padding:14px"><b style="font-size:14px">' + esc(c.name) + '</b>' + (c.product && c.product.origin ? '<span class="region-tag" style="margin-left:8px">' + esc(c.product.origin) + '</span>' : '') +
    '<p style="font-size:12.5px;color:var(--ink2);margin:8px 0;line-height:1.7">' + esc(c.desc) + '</p>' +
    '<button class="btn primary sm" style="width:100%;justify-content:center" data-case="' + i + '">一键套用</button></div></div>'
  ).join("");
  $("cases_grid").querySelectorAll("[data-case]").forEach((b) =>
    b.addEventListener("click", () => applyCase(+b.dataset.case)));
}
async function applyCase(i) {
  const c = SAMPLE_CASES[i];
  if (!c) return;
  $("cases_msg").textContent = "";
  try {
    await createWork(c.name + " 拍摄方案");
    const p = c.product;
    S.data.product = {
      product_name: p.product_name,
      description: ["品类：" + p.category, "产地：" + p.origin, "规格：" + p.spec, "销售方式：" + p.channel].join("；"),
      selling_points: p.points.slice(), price: p.price, audience: "",
      evidence_materials: "",
      shoot: { device: "手机", people: 1, lighting: "", location: "",
        target_duration_seconds: p.duration, note: "" },
    };
    S.data.ideas = [JSON.parse(JSON.stringify(c.idea))];
    S.data.direction = c.idea.name;
    S.ideaSel = 0;
    await saveWork();
    for (const f of c.facts) {
      await ApiClient.post("/api/v1/facts", { org_id: S.org.id,
        content: f.content, category: f.category,
        source_type: "human_statement", source_ref: "示例案例·" + c.name });
    }
    $("cases_modal").classList.remove("show");
    toast("已套用「" + c.name + "」示例（含创意方向与事实草稿）");
    go("v2");
  } catch (err) { $("cases_msg").textContent = err.message || "套用失败"; }
}

/* ---------- #4 更多海报模板 ---------- */
Object.assign(POSTER_TPLS, {
  guochao: { name: "国潮竖版", bg: "linear-gradient(160deg,#0C4229 0%,#0F5132 45%,#7A1F1A 100%)" },
  minimal_h: { name: "简约横版", bg: "linear-gradient(160deg,#F8F4EA 0%,#EFE7D2 100%)", light: true, horizontal: true },
});

/* ---------- #5 海报 AI 生图 ---------- */
let IMG_CONFIGURED = null;
let IMG_QUOTA = null;
let IMG_QUOTA_UNKNOWN = false;
let IMG_AUTH_FAILED = false;
async function checkImageStatus() {
  IMG_AUTH_FAILED = false;
  try {
    const d = V1(await ApiClient.get("/api/v1/poster/image/status"));
    IMG_CONFIGURED = !!(d && d.configured);
  } catch (e) {
    IMG_CONFIGURED = false;
    if (e && (e.status === 401 || e.status === 403)) IMG_AUTH_FAILED = true;
  }
  IMG_QUOTA = null; IMG_QUOTA_UNKNOWN = false;
  if (IMG_CONFIGURED && S.org) {
    try {
      IMG_QUOTA = V1(await ApiClient.get(
        "/api/v1/poster/image/quota?org_id=" + encodeURIComponent(S.org.id)));
    } catch (e) { /* 额度查询失败不挡生图，后端会再校验；但不在界面上假装已知，改走“未知”文案 */ IMG_QUOTA_UNKNOWN = true; }
  }
  const btn = $("btn_gen_poster"), hint = $("ai_img_status");
  if (!btn || !hint) return;
  if (IMG_CONFIGURED) {
    btn.disabled = false; btn.classList.remove("qa-disabled");
    let t = "AI 配图服务可用：输入提示词生成海报背景图。";
    if (IMG_QUOTA && IMG_QUOTA.limited) {
      t += `（今日剩余 ${IMG_QUOTA.remaining} 次）`;
    } else if (IMG_QUOTA_UNKNOWN) {
      t += "（今日剩余额度未知）";
    }
    hint.textContent = t;
  } else {
    btn.disabled = true; btn.classList.add("qa-disabled");
    hint.textContent = IMG_AUTH_FAILED
      ? "登录状态已失效，请重新登录后再生成配图。"
      : "图片生成服务暂时不可用，可先手动排版导出 PNG。";
  }
}
/* O-08：海报排队位置与预计等待展示。eta_seconds 是区间
 * {"min": .., "max": ..}，只作“预计”展示；缺字段时（老后端）
 * 回退通用文案，不报错。 */
function formatEtaSeconds(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " 秒";
  const m = Math.floor(s / 60), r = s % 60;
  return r ? m + " 分 " + r + " 秒" : m + " 分钟";
}
function posterQueueText(st) {
  const pos = (st && typeof st.position === "number") ? st.position : null;
  const eta = st && st.eta_seconds;
  if (pos === null || !eta || typeof eta.min !== "number" || typeof eta.max !== "number") {
    return "AI 正在作画…";
  }
  const range = eta.min === eta.max
    ? "约 " + formatEtaSeconds(eta.min)
    : "约 " + formatEtaSeconds(eta.min) + "～" + formatEtaSeconds(eta.max);
  if (pos <= 0) return "AI 正在作画…（" + range + "）";
  return "AI 正在作画…（前面还有 " + pos + " 个任务，" + range + "）";
}
async function generateImageViaJob(prompt) {
  const body = { org_id: S.org.id, prompt: prompt, size: "1024x1024" };
  let jobId = "";
  try {
    const j = V1(await ApiClient.post("/api/v1/poster/image/jobs", body, { timeoutMs: 30000 }));
    jobId = (j && j.job_id) || "";
    // O-08：提交后立刻按创建响应里的排队位置/预计等待更新提示
    if (jobId) aiLoading(posterQueueText(j));
  } catch (e) {
    // 老后端没有异步接口（404/405）→ 退回同步接口
    if (e && (e.status === 404 || e.status === 405)) {
      const d = V1(await ApiClient.post("/api/v1/poster/image", body, { timeoutMs: 150000 }));
      return d.image_b64 ? ("data:image/png;base64," + d.image_b64) : (d.image_url || "");
    }
    throw e;
  }
  if (!jobId) throw new Error("创建生成任务失败，请重试");
  // 网关对单请求硬超时约 60s，生图常 30-45s：改成轮询，单次请求都很短
  const deadline = Date.now() + 180000;
  let lastErr = "";
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    let st;
    try {
      st = V1(await ApiClient.get("/api/v1/poster/image/jobs/" + encodeURIComponent(jobId),
        { timeoutMs: 30000 }));
    } catch (e) {
      if (e && e.status === 404) throw new Error("生成任务已过期，请重新生成");
      lastErr = (e && e.message) || lastErr;   // 单次轮询抖动不终止
      continue;
    }
    if (st && st.status === "done") {
      // S-06：上游回 url 时走 image_url 分支，不再把 url 拼进 base64
      return st.image_b64 ? ("data:image/png;base64," + st.image_b64) : (st.image_url || "");
    }
    if (st && st.status === "failed") throw new Error(st.error || "图片生成失败");
    // O-08：轮询中按后端返回的排队位置/预计等待更新提示，替代固定文案
    if (st && st.status === "running") aiLoading(posterQueueText(st));
  }
  throw new Error(lastErr || "生成超时（3 分钟），请稍后重试");
}
async function genPosterImage() {
  if (!IMG_CONFIGURED) {
    toast(IMG_AUTH_FAILED ? "登录已过期，请重新登录" : "图片生成服务暂时不可用");
    return;
  }
  const prompt = $("ai_img_prompt").value.trim();
  if (!prompt) { toast("请先输入配图提示词"); return; }
  // O-08：固定文案“约 30-60 秒”已去掉；提交后由 generateImageViaJob
  // 按后端排队位置/预计等待实时更新提示。
  aiLoading("AI 正在作画…");
  try {
    const src = await generateImageViaJob(prompt);
    if (!src) throw new Error("图片服务未返回图片");
    $("ai_img_preview").src = src;
    $("ai_img_result").style.display = "";
    $("btn_use_ai_img").onclick = async () => {
      if (!S.activeWork) { toast("请先创建作品"); return; }
      S.data.poster = collectPoster();
      S.data.poster.ai_image = src;
      await saveWork();
      updatePosterPreview();
      toast("已插入为海报背景");
    };
    toast("配图已生成，可插入为海报背景");
    IMG_QUOTA = IMG_QUOTA && IMG_QUOTA.limited
      ? { ...IMG_QUOTA, remaining: Math.max(0, IMG_QUOTA.remaining - 1), used: IMG_QUOTA.used + 1 }
      : IMG_QUOTA;
    checkImageStatus();
  } catch (e) {
    const msg = String(e.message || "生成失败");
    toast(e.status === 429 || e.code === 2004 || /已用完|次数/.test(msg)
      ? "今日海报生成次数已用完（普通用户每天 5 次），明天再来" : msg);
  }
  finally { aiLoadingDone(); }
}
function drawCoverImage(ctx, dataUrl, W, H) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const sc = Math.max(W / img.width, H / img.height);
      const dw = img.width * sc, dh = img.height * sc;
      ctx.drawImage(img, (W - dw) / 2, (H - dh) / 2, dw, dh);
      resolve(true);
    };
    img.onerror = () => resolve(false);
    img.src = dataUrl;
  });
}

/* ---------- 待接入 5 项：事件绑定 ---------- */
function bindTodo5() {
  $("btn_import_doc").addEventListener("click", () => {
    if (!S.activeWork) { toast("请先创建作品"); return; }
    $("import_doc_file").click();
  });
  $("import_doc_file").addEventListener("change", onImportDocFile);
  $("btn_confirm_import").addEventListener("click", confirmImport);
  $("btn_close_import").addEventListener("click", () => $("import_modal").classList.remove("show"));
  $("qa_import").addEventListener("click", () => { go("v2"); toast("点击「从文档导入」选择 .txt / .md 文件"); });
  $("btn_upload_final").addEventListener("click", () => {
    if (!S.activeWork) { toast("请先创建作品"); return; }
    $("final_video_file").click();
  });
  $("final_video_file").addEventListener("change", uploadFinalVideo);
  $("qa_cases").addEventListener("click", () => {
    $("cases_msg").textContent = "";
    renderCasesGrid();
    $("cases_modal").classList.add("show");
  });
  $("btn_close_cases").addEventListener("click", () => $("cases_modal").classList.remove("show"));
}

/* I-2：使用说明可折叠——首次默认展开，收起后 localStorage 记住（只加折叠，不动内容） */
function initGuide() {
  const body = $("guide_body"), btn = $("guide_toggle");
  if (!body || !btn) return;
  let open = true;
  try { open = localStorage.getItem("lupin_guide_open") !== "0"; } catch (e) { /* 隐私模式等场景按展开处理 */ }
  const apply = () => {
    body.style.display = open ? "" : "none";
    btn.textContent = open ? "收起" : "展开";
    btn.setAttribute("aria-expanded", String(open));
  };
  apply();
  btn.addEventListener("click", () => {
    open = !open;
    try { localStorage.setItem("lupin_guide_open", open ? "1" : "0"); } catch (e) { /* 忽略 */ }
    apply();
  });
}

/* P-4：移动端搜索入口——顶栏图标点开全宽搜索浮层（桌面端该按钮隐藏） */
function initMobileSearch() {
  const btn = $("btn_search_m");
  if (!btn) return;
  const bar = btn.closest(".topbar");
  btn.addEventListener("click", () => {
    const open = bar.classList.toggle("search-open");
    btn.setAttribute("aria-expanded", String(open));
    if (open) { const inp = $("global_search"); if (inp) inp.focus(); }
  });
}

/* ================= 事件绑定 ================= */
/* 移动端导航抽屉：复用同一套 .sidebar，不另建一份导航
   注意 V3 会在 bindAll 里动态注入 v7/v8/v9 导航项，所以用事件委托而不是逐个绑定 */
function bindMobileNav() {
  const btn = $("btn_nav"), side = $("sidebar"), bd = $("nav_backdrop"), close = $("btn_nav_close");
  if (!btn || !side) return;
  const isOpen = () => document.body.classList.contains("nav-open");
  const setOpen = (open) => {
    document.body.classList.toggle("nav-open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
    if (open && close) close.focus({ preventScroll: true });
  };
  btn.addEventListener("click", () => setOpen(!isOpen()));
  if (close) close.addEventListener("click", () => setOpen(false));
  if (bd) bd.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) setOpen(false);
  });
  side.addEventListener("click", (e) => {
    if (isOpen() && e.target.closest(".nav-item")) setOpen(false);
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 760 && isOpen()) setOpen(false);
  });
}

function bindAll() {
bindMobileNav(); // 移动端抽屉导航：必须在注入 v7/v8/v9 之前挂好容器
initCopyOptView(); // V3.0 D：注入文案优化器导航与视图（零 HTML 改动）
initPlanSection(); // V3.0 C：注入内容策划面板（零 HTML 改动）
initPublishView(); // V3.0 F：注入发布中心导航与视图（零 HTML 改动）
initLibView(); // V3.0 G：注入素材库导航与视图（零 HTML 改动）
initAgentBindings(); // Phase 4b：AI 创作助手事件绑定
document.querySelectorAll(".nav-item[data-v]").forEach((b) => b.addEventListener("click", () => go(b.dataset.v)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => go(b.dataset.goto)));
document.querySelectorAll("[data-todo]").forEach((b) => b.addEventListener("click", () => todo(b.dataset.todo)));

$("side_user").addEventListener("click", () => { if (!loggedIn()) go("v-login");});
$("tab_login").addEventListener("click", () => setAuthMode("login"));
$("tab_register").addEventListener("click", () => setAuthMode("register"));
$("btn_do_auth").addEventListener("click", doAuth);
$("login_password").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth();});
$("btn_logout").addEventListener("click", doLogout);

const newWork = () => newWorkFlow();
bindTodo5();
initGlobalSearch();
// I-2：新建入口收敛——只留顶栏按钮（工作台快捷卡与作品区按钮已撤，空状态另有 CTA）
$("btn_new_work_top").addEventListener("click", newWork);
initGuide();
initMobileSearch();
document.querySelectorAll("#v1 [data-filter]").forEach((p) => { keyActivate(p); p.addEventListener("click", () => {
S.workFilter = p.dataset.filter;
document.querySelectorAll("#v1 [data-filter]").forEach((x) => {
const on = x === p;
x.classList.toggle("blue", on); x.classList.toggle("gray",!on);
});
renderWorkbench();
}); });

$("btn_save_product").addEventListener("click", saveProduct);
$("btn_to_v3").addEventListener("click", async () => { if (await saveProduct()) go("v3");});
$("btn_add_fact").addEventListener("click", () => { $("fact_editor").style.display = ""; errText("fact_err", "");});
$("btn_cancel_fact").addEventListener("click", () => { $("fact_editor").style.display = "none";});
$("btn_save_fact").addEventListener("click", saveFact);

$("btn_gen_ideas").addEventListener("click", genIdeas);
$("btn_regen_ideas").addEventListener("click", genIdeas);
$("btn_gen_storyboard").addEventListener("click", genStoryboard);
$("btn_regen_all").addEventListener("click", genStoryboard);
$("btn_add_shot").addEventListener("click", addShot);
$("tab_risk").addEventListener("click", () => {
$("tab_risk").classList.add("active"); $("tab_feas").classList.remove("active");
$("pane_risk").style.display = ""; $("pane_feas").style.display = "none";
});
$("tab_feas").addEventListener("click", () => {
$("tab_feas").classList.add("active"); $("tab_risk").classList.remove("active");
$("pane_feas").style.display = ""; $("pane_risk").style.display = "none";
});
$("btn_run_risk").addEventListener("click", runRisk);
// V2.1：风险检查标脏徽章（JS 注入，不动 HTML）
$("btn_run_risk").insertAdjacentHTML("afterend",
  '<span id="risk_dirty_badge" style="display:none;font-size:12.5px;color:#92400e;background:#fef3c7;border:1px solid #fcd34d;border-radius:999px;padding:4px 10px">文案已修改，风险检查需重新运行</span>');
$("btn_run_feas").addEventListener("click", runFeas);

document.querySelectorAll("#poster_tpls .t").forEach((t) => t.addEventListener("click", () => {
document.querySelectorAll("#poster_tpls .t").forEach((x) => x.classList.remove("active"));
t.classList.add("active");
updatePosterPreview();
}));
["pe_title", "pe_sub", "pe_promo", "pe_btn"].forEach((id) => $(id).addEventListener("input", updatePosterPreview));
$("btn_gen_poster").addEventListener("click", genPosterImage);
$("btn_poster_save").addEventListener("click", savePosterDraft);
$("btn_poster_png").addEventListener("click", exportPosterPNG);
$("btn_copy_copy").addEventListener("click", async () => {
const txt = $("pe_copy").value;
if (!txt.trim()) { toast("文案为空"); return;}
try { await navigator.clipboard.writeText(txt); toast("已复制到剪贴板");}
catch (e) { toast("复制失败，请手动选择复制");}
});

$("btn_print").addEventListener("click", () => window.print());
injectP1UI();
$("btn_upload_asset").addEventListener("click", () => {
if (!S.activeWork) { toast("请先创建作品"); return;}
$("file_asset").click();
});
$("file_asset").addEventListener("change", (e) => {
const f = e.target.files[0];
e.target.value = "";
if (f) uploadAsset(f);
});
$("btn_save_note").addEventListener("click", async () => {
if (!S.activeWork) { toast("请先创建作品"); return;}
S.data.shoot_note = $("shoot_note").value;
await saveWork();
toast("备注已保存");
});
$("btn_gen_live").addEventListener("click", genLivestreamScript);
$("btn_export_live").addEventListener("click", exportLivestreamTxt);
$("btn_close_colloquial").addEventListener("click", () => $("colloquial_modal").classList.remove("show"));
$("colloquial_modal").addEventListener("click", (e) => { if (e.target === $("colloquial_modal")) $("colloquial_modal").classList.remove("show"); });
$("btn_use_colloquial").addEventListener("click", useColloquial);
$("btn_undo_colloquial").addEventListener("click", undoColloquial);
$("btn_force_colloquial").addEventListener("click", () => doUseColloquial(true));
$("btn_cancel_colloquial").addEventListener("click", () => { $("colloq_conflict").style.display = "none"; });

$("btn_delivery_check").addEventListener("click", runDeliveryCheck);
$("exp_docx").addEventListener("click", exportDocx);
$("exp_srt").addEventListener("click", exportSrt);
$("exp_txt").addEventListener("click", exportTxt);
$("btn_export_all").addEventListener("click", async () => {
if (!needStoryboard()) return;
await exportDocx(); await exportSrt(); exportTxt();
});

$("nav_api_cfg").addEventListener("click", (e) => openCfg(e.currentTarget));
// 保存/清除/关闭/遮罩点击已由 openCfg 内的动态弹窗绑定（_openUiModal 自带遮罩与 Esc 逻辑），此处不再挂旧静态监听。
}

/* ================= 初始化 ================= */
async function init() {
bindAll();
S.data = blankData();
if (AuthClient && AuthClient.onSessionExpired) {
AuthClient.onSessionExpired(() => { toast("登录已过期，请重新登录"); go("v-login");});
}
// 尝试用 httpOnly Cookie 恢复会话
let restored = false;
try {
if (await AuthClient.tryRefresh()) {
const me = await AuthClient.me();
if (me) { S.user = me; restored = true;}
}
} catch (e) { /* 忽略，走登录页 */}
if (restored) {
try {
await ensureOrg();
renderUser();
go("v1");
} catch (e) {
go("v0");
}
} else {
renderUser();
// /login 入口：未登录时直接落在登录视图，而不是首页
go(location.pathname === "/login" ? "v-login" : "v0");
}
revealIn($("v0"));
initShootCondForm();
}

/* V3.0 D：文案优化器样式（JS 注入，零 HTML 改动） */
function initCopyOptCss() {
  if (document.getElementById("copyopt_css")) return;
  const st = document.createElement("style");
  st.id = "copyopt_css";
  st.textContent = `
.copy-wrap{display:grid;grid-template-columns:290px 1fr;gap:14px;align-items:start}
/* V3.2 平板适配：961–1240px（iPad 横屏等）收窄左栏，右侧编辑区不再被挤成窄条 */
@media(max-width:1240px){.copy-wrap{grid-template-columns:250px 1fr}}
@media(max-width:960px){.copy-wrap{grid-template-columns:1fr}}
.copy-doc-item{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:8px}
.copy-doc-item.active{border-color:var(--primary);box-shadow:var(--shadow)}
.copy-ver{border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px}
.copy-ver.is-draft{border-style:dashed}
.copy-ver-body{margin:8px 0;padding:10px;background:#FAF7EF;border-radius:8px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
/* 全局 input/select 是 width:100%，进 flex 行后会把按钮挤成两字一行（v8 发布中心）
   这里给行内控件弹性宽度、给按钮固定宽度 */
.row>.input,.row>input,.row>select{flex:1 1 160px;min-width:0;width:auto}
.row>.btn,.row>button.btn{flex:0 0 auto;white-space:nowrap}
.row>.fld{flex:1 1 180px;min-width:0}
.fld{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.fld>span{font-size:12.5px;color:var(--muted)}
.copy-opts{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
@media(max-width:640px){.copy-opts{grid-template-columns:1fr}}
.cmp-box{border:1px solid var(--line);border-radius:8px;padding:10px;min-height:80px}
.dl{padding:2px 6px;border-radius:4px;margin:1px 0}
.dl.same{color:var(--muted)}
.dl.del{background:#fde8e8;color:#B03232}
.dl.add{background:#e6f7ee;color:#147A56}
.copy-rw{border-top:1px dashed var(--line);margin-top:8px}`;
  document.head.appendChild(st);
}
/* ================= V3.0 D：v7 文案优化器（独立视图，零 HTML 改动） ================= */
const COPY_KINDS = [
  ["optimize", "优化已有文案"], ["generate", "从零生成新文案"],
  ["script", "完整视频脚本"], ["platform", "平台发布文案"], ["adapt", "平台适配调整"],
];
/* V3.0 E：多平台内容适配 */
const PLATFORM_IDS = [
  ["douyin", "抖音"], ["kuaishou", "快手"],
  ["xiaohongshu", "小红书"], ["shipinhao", "视频号"],
];
const PLATFORM_DISCLAIMER = "平台规则可能变化，以下为常见公开配置整理，仅供参考；发布前请以各平台官方最新规范为准。本工具不承诺任何流量效果。";
const COPY_MODES = [
  ["natural", "自然口语化"], ["premium", "品牌高级感"], ["shandong", "山东本土表达"],
  ["ecommerce", "电商转化表达"], ["hook", "开头吸引力优化"], ["concise", "结构精简"],
  ["voiceover", "旁白口播优化"], ["platform_post", "发布文案优化"], ["custom", "自定义要求"],
];
const COPY = { docs: [], doc: null, compareId: null, busy: false, products: [], rewriteFor: null };

function initCopyOptView() {
  initCopyOptCss();
  if (!document.querySelector('.nav-item[data-v="v7"]')) {
    const navV6 = document.querySelector('.nav-item[data-v="v6"]');
    const b = document.createElement("button");
    b.className = "nav-item"; b.dataset.v = "v7";
    b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>文案优化';
    b.addEventListener("click", () => go("v7"));
    if (navV6) navV6.after(b);
  }
  if (!$("v7") && $("v6")) {
    const s = document.createElement("section");
    s.className = "view"; s.id = "v7";
    $("v6").after(s);
  }
}

function copyKindLabel(k) { const f = COPY_KINDS.find((x) => x[0] === k); return f ? f[1] : k; }
function copyModeLabel(m) { const f = COPY_MODES.find((x) => x[0] === m); return f ? f[1] : m; }

function copyRiskBadge(v) {
  if (v.risk_status === "checked") {
    const n = (v.risk_flags || []).length;
    return `<span class="pill green" title="${esc((v.risk_flags || []).map((f) => f.text || f.reason || "").join("\n"))}">已检查${n ? "·" + n + "项提醒" : ""}</span>`;
  }
  if (v.risk_status === "failed") return '<span class="pill amber">风险检查未完成</span>';
  return '<span class="pill">未检查</span>';
}

/* 视图首屏骨架屏：进入视图时先渲染加载态，数据到了再替换成真实内容 */
function viewSkeleton(headTitle, headSub) {
  return `<div class="page-head"><div class="skel skel-head" aria-hidden="true"></div>
    ${headSub ? `<div class="skel skel-sub" aria-hidden="true"></div>` : ""}<div class="page-meander" style="opacity:.22;margin-bottom:14px" aria-hidden="true"></div></div>
    <div class="skel-body" aria-busy="true" aria-label="加载中">
    <div class="skel-flower" aria-hidden="true">${FLOWER_SVG}</div>
    <div class="skel skel-row" aria-hidden="true"></div><div class="skel skel-row" aria-hidden="true"></div><div class="skel skel-row" aria-hidden="true"></div>
    </div>`;
}

async function renderCopyOpt() {
  const sec = $("v7");
  if (!sec) return;
  if (!S.org) { sec.innerHTML = '<div class="page-head"><h2>文案优化器</h2><p class="muted">请先登录并选择组织</p><div class="page-meander" aria-hidden="true"></div></div>'; return; }
  sec.innerHTML = viewSkeleton(); // 先给加载态：线上网关延迟下不能留白
  await loadCopyDocs();
  sec.innerHTML = `
    <div class="page-head"><h2>文案优化器</h2>
    <p class="muted">优化已有文案 / 从零生成 / 视频脚本 / 发布文案 / 平台适配 —— 未经你确认，不会覆盖任何已确认版本</p><div class="page-meander" aria-hidden="true"></div></div>
    <div class="copy-wrap">
      <div class="copy-side">
        <div class="card"><h3>文案文档</h3><div id="copy_doc_list"></div>
          <div class="copy-new">
            <input id="copy_new_title" class="input" placeholder="文档标题，如：章丘大葱 · 抖音口播">
            <div class="row">
              <select id="copy_new_kind" class="input">${COPY_KINDS.map((k) => `<option value="${k[0]}">${k[1]}</option>`).join("")}</select>
              <button class="btn primary sm" id="copy_new_btn">＋ 新建</button>
            </div>
          </div></div>
      </div>
      <div class="copy-main"><div id="copy_editor"></div></div>
    </div>`;
  $("copy_new_btn").addEventListener("click", createCopyDoc);
  renderCopyDocList();
  renderCopyEditor();
  /* V3.2：有文档但未打开时自动打开第一篇——平板/首屏不再出现"右侧整块空态" */
  if (!COPY.doc && COPY.docs.length) await openCopyDoc(COPY.docs[0].id);
}

async function loadCopyDocs() {
  try {
    const r = V1(await ApiClient.get("/api/v1/copy/docs?org_id=" + encodeURIComponent(S.org.id)));
    COPY.docs = (r && r.docs) || [];
  } catch (e) { COPY.docs = []; }
}

async function createCopyDoc() {
  const title = $("copy_new_title").value.trim() || "未命名文案";
  const kind = $("copy_new_kind").value;
  try {
    const d = V1(await ApiClient.post("/api/v1/copy/docs?org_id=" + encodeURIComponent(S.org.id), { title, kind }));
    COPY.docs.unshift({ id: d.id, title: d.title, kind: d.kind, version_count: 0, current_version_no: null });
    await openCopyDoc(d.id);
    toast("已创建");
  } catch (e) { toast("创建失败：" + (e.message || e)); }
}

async function openCopyDoc(id) {
  try {
    COPY.doc = V1(await ApiClient.get(`/api/v1/copy/docs/${id}?org_id=` + encodeURIComponent(S.org.id)));
    COPY.compareId = null; COPY.rewriteFor = null;
    renderCopyDocList(); renderCopyEditor();
  } catch (e) { toast("打开失败：" + (e.message || e)); }
}

async function deleteCopyDoc(id) {
  if (!(await modalConfirm({ title: "删除文案文档", body: "确定删除整个文案文档（含全部版本）？此操作不可恢复。", okText: "删除", danger: true }))) return;
  try {
    await ApiClient.del(`/api/v1/copy/docs/${id}?org_id=` + encodeURIComponent(S.org.id));
    COPY.docs = COPY.docs.filter((d) => d.id !== id);
    if (COPY.doc && COPY.doc.id === id) COPY.doc = null;
    renderCopyDocList(); renderCopyEditor(); toast("已删除");
  } catch (e) { toast("删除失败：" + (e.message || e)); }
}

function renderCopyDocList() {
  const el = $("copy_doc_list");
  if (!el) return;
  if (!COPY.docs.length) { el.innerHTML = emptyState("还没有文案文档", "新建一篇，从口播稿或发布文案开始。", true, { label: "新建文档", fn: () => { const t = $("copy_new_title"); if (t) { t.focus(); t.scrollIntoView({ block: "center", behavior: "smooth" }); } } }); return; }
  // V3.7：文档按类型归拢分组（文案文档无作品归属，按 5 种工作类型分组）
  const groups = [];
  COPY_KINDS.forEach(([k, label]) => {
    const items = COPY.docs.filter((d) => (d.kind || "") === k);
    if (items.length) groups.push({ title: label, items });
  });
  const rest = COPY.docs.filter((d) => !COPY_KINDS.some((x) => x[0] === (d.kind || "")));
  if (rest.length) groups.push({ title: "其他类型", items: rest });
  const docHtml = (d) => `
    <div class="copy-doc-item ${COPY.doc && COPY.doc.id === d.id ? "active" : ""}" data-id="${d.id}">
      <div><b>${esc(d.title)}</b></div>
      <div class="muted" style="font-size:12.5px">${d.version_count || 0} 个版本${d.current_version_no ? " · 当前 v" + d.current_version_no : ""}</div>
      <div class="row" style="gap:6px;margin-top:6px">
        <button class="btn ghost sm" data-act="open">打开</button>
        <button class="btn ghost sm" data-act="del">删除</button>
      </div></div>`;
  el.innerHTML = groups.map((g) => `
    <div class="wgroup" style="margin-bottom:16px">
      <div class="wgroup-head" style="margin-bottom:8px">
        <h3 style="font-size:13px"><span class="wg-name">${esc(g.title)}</span></h3>
        <span class="pill gray">${g.items.length} 篇</span>
        <span class="wg-line"></span>
      </div>
      ${g.items.map(docHtml).join("")}
    </div>`).join("");
  el.querySelectorAll(".copy-doc-item").forEach((item) => {
    const id = item.dataset.id;
    item.querySelector('[data-act="open"]').addEventListener("click", () => openCopyDoc(id));
    item.querySelector('[data-act="del"]').addEventListener("click", () => deleteCopyDoc(id));
  });
}

function copyVersionText(v) {
  if (v.structured && v.structured.body) return v.structured.body;
  return v.text || "";
}

function renderCopyEditor() {
  const el = $("copy_editor");
  if (!el) return;
  const doc = COPY.doc;
  if (!doc) { el.innerHTML = '<div class="card"><p class="muted">← 从左侧选择或新建文案文档</p></div>'; return; }
  const cur = doc.current_version;
  const versions = (doc.versions || []).slice().reverse(); // 新在前
  el.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <h3>${esc(doc.title)} <span class="pill">${esc(copyKindLabel(doc.kind))}</span>
        ${cur ? `<span class="pill green">当前 v${cur.version_no}</span>` : '<span class="pill">暂无确认版本</span>'}</h3>
      </div>
      <label class="fld"><span>原文输入（直接粘贴，或从下方导入）</span>
        <textarea id="copy_source" class="input" rows="5" placeholder="粘贴要优化的文案…（“从零生成”类型可留空）"></textarea></label>
      <div class="row" style="gap:6px;flex-wrap:wrap;margin:8px 0">
        <button class="btn ghost sm" id="copy_imp_vo">从旁白导入</button>
        <button class="btn ghost sm" id="copy_imp_script">从脚本导入</button>
        <button class="btn ghost sm" id="copy_imp_cur">载入当前版本</button>
      </div>
      <div class="copy-opts">
        <label class="fld"><span>优化模式</span><select id="copy_mode" class="input">${COPY_MODES.map((m) => `<option value="${m[0]}">${m[1]}</option>`).join("")}</select></label>
        <label class="fld"><span>优化目标</span><input id="copy_goal" class="input" placeholder="如：让开头更抓人"></label>
        <label class="fld"><span>风格约束</span><input id="copy_style" class="input" placeholder="如：实在、不端着"></label>
        <label class="fld"><span>目标受众 / 平台</span><input id="copy_audience" class="input" placeholder="如：抖音"></label>
        <label class="fld"><span>长度要求</span><input id="copy_length" class="input" placeholder="如：100字以内"></label>
        <label class="fld"><span>关联产品（可选，带入已确认事实）</span><select id="copy_product" class="input"><option value="">不关联</option></select></label>
      </div>
      <label class="fld"><span>额外要求</span><input id="copy_extra" class="input" placeholder="其他补充要求"></label>
      <label class="fld" id="copy_custom_wrap" style="display:none"><span>自定义优化要求</span><textarea id="copy_custom" class="input" rows="2"></textarea></label>
      <div class="row" style="gap:8px;margin-top:10px">
        <button class="btn primary" id="copy_gen_btn">生成优化版本</button>
        <span class="muted" style="font-size:12.5px">生成结果先存为草稿，你确认后才生效；原文不受影响</span>
      </div>
    </div>
    <div class="card" style="margin-top:12px"><h3>版本历史（${doc.versions.length}）</h3><div id="copy_versions"></div></div>
    <div class="card" id="copy_compare_card" style="margin-top:12px;display:none"><h3>版本对比</h3><div id="copy_compare"></div></div>
    <div class="notice" style="margin-top:12px">风险检查通过不代表广告合规得到保证。发布前请人工确认文案中的产品宣称、价格与承诺事项，本工具不保证广告合规。</div>`;
  $("copy_mode").addEventListener("change", () => {
    $("copy_custom_wrap").style.display = $("copy_mode").value === "custom" ? "" : "none";
  });
  $("copy_gen_btn").addEventListener("click", runCopyOptimize);
  $("copy_imp_vo").addEventListener("click", () => importCopyFromWork(false));
  $("copy_imp_script").addEventListener("click", () => importCopyFromWork(true));
  $("copy_imp_cur").addEventListener("click", () => {
    if (cur) $("copy_source").value = copyVersionText(cur);
    else toast("还没有已确认版本");
  });
  loadCopyProducts();
  renderCopyVersions();
  renderCopyCompare();
}

async function loadCopyProducts() {
  const sel = $("copy_product");
  if (!sel) return;
  try {
    const r = V1(await ApiClient.get("/api/v1/products?org_id=" + encodeURIComponent(S.org.id)));
    const list = (r && (r.items || r.products)) || r || [];
    (Array.isArray(list) ? list : []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name || p.code;
      sel.appendChild(o);
    });
  } catch (e) { /* 产品列表加载失败不阻断 */ }
}

function importCopyFromWork(withSubtitle) {
  const shots = ((S.data || {}).storyboard || {}).shots || [];
  if (!shots.length) { toast("当前作品没有分镜，先去创意策划生成"); return; }
  const parts = shots.map((s) => withSubtitle
    ? `【镜头${s.shot_number}】${s.voiceover || ""}\n${s.subtitle || ""}`
    : (s.voiceover || ""));
  $("copy_source").value = parts.filter(Boolean).join("\n\n");
  toast("已导入 " + shots.length + " 个镜头");
}

async function runCopyOptimize() {
  if (COPY.busy) return;
  const doc = COPY.doc;
  const btn = $("copy_gen_btn");
  const body = {
    mode: $("copy_mode").value,
    source_text: $("copy_source").value,
    goal: $("copy_goal").value.trim(),
    style: $("copy_style").value.trim(),
    audience: $("copy_audience").value.trim(),
    length_limit: $("copy_length").value.trim(),
    extra_requirements: $("copy_extra").value.trim(),
    custom_instruction: $("copy_custom") ? $("copy_custom").value.trim() : "",
    product_id: $("copy_product").value || null,
  };
  COPY.busy = true; btn.disabled = true; btn.textContent = "生成中…";
  aiLoading("AI 文案生成中…");
  try {
    const r = V1(await ApiClient.post(
      `/api/v1/copy/docs/${doc.id}/optimize?org_id=` + encodeURIComponent(S.org.id),
      body, { idempotencyKey: "auto" }));
    await openCopyDoc(doc.id); // 刷新版本列表
    toast("已生成草稿版本 v" + r.version.version_no + "，请对比确认");
    if (r.review_reminder) toast(r.review_reminder, 6000);
  } catch (e) { toast("生成失败：" + (e.message || e)); }
  finally { COPY.busy = false; btn.disabled = false; btn.textContent = "生成优化版本"; aiLoadingDone(); }
}

function renderCopyVersions() {
  const el = $("copy_versions");
  if (!el || !COPY.doc) return;
  const versions = (COPY.doc.versions || []).slice().reverse();
  el.innerHTML = versions.map((v) => {
    const st = v.structured || {};
    const bodyHtml = st.body
      ? `<div><b>${esc(st.title || "")}</b></div><div>${esc(st.body).replace(/\n/g, "<br>")}</div>
         <div class="muted" style="font-size:12.5px">${esc((st.hashtags || []).join(" "))} · ${esc(st.cta || "")}</div>`
      : `<div>${esc(v.text).replace(/\n/g, "<br>")}</div>`;
    const isDraft = v.status === "draft";
    return `<div class="copy-ver ${isDraft ? "is-draft" : ""}">
      <div class="row" style="justify-content:space-between;align-items:center">
        <div><b>v${v.version_no}</b> <span class="pill">${esc(copyModeLabel(v.mode))}</span>
          ${isDraft ? '<span class="pill amber">草稿待确认</span>' : '<span class="stamp-badge">已确认</span>'}
          ${copyRiskBadge(v)}
          ${v.restored_from ? '<span class="pill">恢复版本</span>' : ""}</div>
        <div class="row" style="gap:6px">
          <button class="btn ghost sm" data-a="cmp" data-id="${v.id}">对比</button>
          ${isDraft ? `<button class="btn primary sm" data-a="ok" data-id="${v.id}">确认采用</button>
            <button class="btn ghost sm" data-a="drop" data-id="${v.id}">放弃草稿</button>` : ""}
          ${!isDraft ? `<button class="btn ghost sm" data-a="back" data-id="${v.id}">恢复此版</button>
            <button class="btn ghost sm" data-a="plat" data-id="${v.id}">平台版本</button>` : ""}
          <button class="btn ghost sm" data-a="rw" data-id="${v.id}">段落重写</button>
          <button class="btn ghost sm" data-a="load" data-id="${v.id}">载入为原文</button>
        </div></div>
      <div class="copy-ver-body">${bodyHtml}</div>
      ${v.changes_summary ? `<div class="muted" style="font-size:12.5px;white-space:pre-line">改动说明：\n${esc(v.changes_summary)}</div>` : ""}
      ${v.risk_status === "checked" && (v.risk_flags || []).length ? `<div class="muted" style="font-size:12.5px;color:#a16207">风险提醒：${esc(v.risk_flags.map((f) => f.text || f.reason || "").filter(Boolean).join("；"))}</div>` : ""}
      <div class="copy-rw" id="rw_${v.id}" style="display:none"></div>
      <div class="copy-plat" id="plat_${v.id}" style="display:none"></div>
    </div>`;
  }).join("");
  el.querySelectorAll("button[data-a]").forEach((b) => {
    const id = b.dataset.id, a = b.dataset.a;
    if (a === "cmp") b.addEventListener("click", () => { COPY.compareId = id; renderCopyCompare(); });
    else if (a === "ok") b.addEventListener("click", () => confirmCopyVersion(id));
    else if (a === "drop") b.addEventListener("click", () => dropCopyVersion(id));
    else if (a === "back") b.addEventListener("click", () => restoreCopyVersion(id));
    else if (a === "rw") b.addEventListener("click", () => showRewritePanel(id));
    else if (a === "plat") b.addEventListener("click", () => showPlatformPanel(id));
    else if (a === "load") b.addEventListener("click", () => {
      const v = COPY.doc.versions.find((x) => x.id === id);
      if (v && $("copy_source")) { $("copy_source").value = copyVersionText(v); toast("已载入编辑区"); }
    });
  });
}

async function confirmCopyVersion(id) {
  try {
    const r = V1(await ApiClient.post(
      `/api/v1/copy/docs/${COPY.doc.id}/versions/${id}/confirm?org_id=` + encodeURIComponent(S.org.id), {}));
    await openCopyDoc(COPY.doc.id);
    toast("已确认为当前版本 v" + r.current_version.version_no);
    stampPop(document.querySelector(`.copy-ver [data-id="${id}"]`)?.closest(".copy-ver"), "已确认");
  } catch (e) { toast("确认失败：" + (e.message || e)); }
}

async function dropCopyVersion(id) {
  if (!(await modalConfirm({ title: "放弃草稿版本", body: "确定放弃这个草稿版本吗？\n此操作不可恢复。", okText: "放弃", danger: true }))) return;
  try {
    await ApiClient.del(`/api/v1/copy/docs/${COPY.doc.id}/versions/${id}?org_id=` + encodeURIComponent(S.org.id));
    await openCopyDoc(COPY.doc.id); toast("已放弃草稿");
  } catch (e) { toast("操作失败：" + (e.message || e)); }
}

async function restoreCopyVersion(id) {
  const v = COPY.doc.versions.find((x) => x.id === id);
  if (!(await modalConfirm({ title: "恢复版本", body: `确定恢复到 v${v ? v.version_no : "?"}？将新建一个已确认版本，现有版本不受影响。`, okText: "恢复" }))) return;
  try {
    const r = V1(await ApiClient.post(
      `/api/v1/copy/docs/${COPY.doc.id}/versions/${id}/restore?org_id=` + encodeURIComponent(S.org.id), {}));
    await openCopyDoc(COPY.doc.id);
    toast("已恢复为 v" + r.version.version_no);
  } catch (e) { toast("恢复失败：" + (e.message || e)); }
}

function splitParas(t) { return String(t || "").split(/\n\s*\n|\n/).map((s) => s.trim()).filter(Boolean); }

function showRewritePanel(versionId) {
  const v = COPY.doc.versions.find((x) => x.id === versionId);
  const box = $("rw_" + versionId);
  if (!v || !box) return;
  const paras = splitParas(copyVersionText(v));
  if (!paras.length) { toast("该版本没有可重写的段落"); return; }
  box.style.display = "";
  box.innerHTML = `
    <div class="row" style="gap:6px;flex-wrap:wrap;margin-top:8px">
      <select id="rw_sel_${versionId}" class="input" style="flex:2;min-width:160px">
        ${paras.map((p, i) => `<option value="${i}">第${i + 1}段：${esc(p.slice(0, 30))}${p.length > 30 ? "…" : ""}</option>`).join("")}
      </select>
      <input id="rw_ins_${versionId}" class="input" placeholder="重写要求（默认：自然口语化）" style="flex:2;min-width:160px">
      <button class="btn primary sm" id="rw_go_${versionId}">只重写该段</button>
      <button class="btn ghost sm" id="rw_x_${versionId}">取消</button>
    </div>
    <p class="muted" style="font-size:12.5px">只改所选段落，其他段落原样保留，结果存为新草稿</p>`;
  $("rw_x_" + versionId).addEventListener("click", () => { box.style.display = "none"; });
  $("rw_go_" + versionId).addEventListener("click", async () => {
    const idx = parseInt($("rw_sel_" + versionId).value, 10);
    const ins = $("rw_ins_" + versionId).value.trim();
    aiLoading("段落重写中…");
    try {
      const r = V1(await ApiClient.post(
        `/api/v1/copy/docs/${COPY.doc.id}/rewrite-paragraph?org_id=` + encodeURIComponent(S.org.id),
        { version_id: versionId, paragraph_index: idx, instruction: ins },
        { idempotencyKey: "auto" }));
      await openCopyDoc(COPY.doc.id);
      toast("已生成新草稿 v" + r.version.version_no);
    } catch (e) { toast("重写失败：" + (e.message || e)); }
    finally { aiLoadingDone(); }
  });
}

/* 行级 diff：old→new，删除红、新增绿 */
function copyLineDiff(oldT, newT) {
  const A = String(oldT || "").split("\n"), B = String(newT || "").split("\n");
  const m = A.length, n = B.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) for (let j = n - 1; j >= 0; j--)
    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  let i = 0, j = 0, html = "";
  while (i < m && j < n) {
    if (A[i] === B[j]) { html += `<div class="dl same">${esc(A[i]) || "&nbsp;"}</div>`; i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { html += `<div class="dl del">− ${esc(A[i])}</div>`; i++; }
    else { html += `<div class="dl add">+ ${esc(B[j])}</div>`; j++; }
  }
  while (i < m) { html += `<div class="dl del">− ${esc(A[i])}</div>`; i++; }
  while (j < n) { html += `<div class="dl add">+ ${esc(B[j])}</div>`; j++; }
  return html;
}

function renderCopyCompare() {
  const card = $("copy_compare_card"), el = $("copy_compare");
  if (!card || !el) return;
  const doc = COPY.doc;
  const v = doc && (doc.versions || []).find((x) => x.id === COPY.compareId);
  const cur = doc && doc.current_version;
  if (!v || !cur || v.id === cur.id) {
    card.style.display = "none"; el.innerHTML = ""; return;
  }
  card.style.display = "";
  el.innerHTML = `
    <div class="row" style="gap:12px;align-items:flex-start">
      <div style="flex:1"><h4>当前版本 v${cur.version_no}（已确认）</h4>
        <div class="cmp-box">${esc(copyVersionText(cur)).replace(/\n/g, "<br>")}</div></div>
      <div style="flex:1"><h4>对比版本 v${v.version_no}（${v.status === "draft" ? "草稿" : "已确认"}）</h4>
        <div class="cmp-box">${esc(copyVersionText(v)).replace(/\n/g, "<br>")}</div></div>
    </div>
    <h4 style="margin-top:10px">差异（红删 / 绿增）</h4>
    <div class="cmp-diff">${copyLineDiff(copyVersionText(cur), copyVersionText(v))}</div>
    <div class="row" style="gap:8px;margin-top:10px">
      ${v.status === "draft" ? `<button class="btn primary sm" id="cmp_ok">确认采用 v${v.version_no}</button>` : ""}
      <button class="btn ghost sm" id="cmp_x">关闭对比</button>
    </div>`;
  $("cmp_x").addEventListener("click", () => { COPY.compareId = null; renderCopyCompare(); });
  const ok = $("cmp_ok");
  if (ok) ok.addEventListener("click", () => confirmCopyVersion(v.id));
}
/* ================= V3.0 E：多平台内容适配（v7 文案优化器内，零 HTML 改动） ================= */
function platName(pid) {
  const f = PLATFORM_IDS.find((x) => x[0] === pid);
  return f ? f[1] : pid;
}

async function showPlatformPanel(versionId) {
  const box = $("plat_" + versionId);
  if (!box || !COPY.doc) return;
  const v = COPY.doc.versions.find((x) => x.id === versionId);
  if (!v) return;
  const showing = box.style.display !== "none";
  document.querySelectorAll(".copy-plat").forEach((d) => { d.style.display = "none"; });
  if (showing) return;
  box.style.display = "";
  box.innerHTML = `
    <div class="card" style="margin-top:10px;background:#fafaf9">
      <h4>平台版本 <span class="muted" style="font-weight:normal">（源自 v${v.version_no}，各平台版本独立保存）</span></h4>
      <p class="muted" style="font-size:12.5px">${esc(PLATFORM_DISCLAIMER)}</p>
      <div class="row" style="gap:6px;flex-wrap:wrap;margin:8px 0">
        <select id="plat_sel_${versionId}" class="input" style="min-width:120px">
          ${PLATFORM_IDS.map((p) => `<option value="${p[0]}">${p[1]}</option>`).join("")}
        </select>
        <button class="btn primary sm" id="plat_ai_${versionId}">AI 生成该平台版本</button>
        <button class="btn ghost sm" id="plat_manual_${versionId}">手动填写</button>
      </div>
      <div id="plat_form_${versionId}"></div>
      <div id="plat_list_${versionId}"><p class="muted">加载中…</p></div>
    </div>`;
  $("plat_ai_" + versionId).addEventListener("click", () => genPlatformVersion(versionId, true));
  $("plat_manual_" + versionId).addEventListener("click", () => showPlatformForm(versionId, false));
  await loadPlatformList(versionId);
}

async function loadPlatformList(versionId) {
  const el = $("plat_list_" + versionId);
  if (!el || !COPY.doc) return;
  try {
    const r = V1(await ApiClient.get(
      `/api/v1/platforms/copy/docs/${COPY.doc.id}/platforms?org_id=` + encodeURIComponent(S.org.id)));
    const items = r.items || [];
    if (!items.length) { el.innerHTML = emptyState("还没有平台版本", "选好平台后生成一份适配版本。", true, { label: "去生成", fn: () => { const sel = document.querySelector("#v7 select"); if (sel) { sel.focus(); sel.scrollIntoView({ block: "center", behavior: "smooth" }); } } }); return; }
    el.innerHTML = items.map((p) => `
      <div class="copy-ver" style="margin-top:8px">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div><b>${esc(platName(p.platform))}</b> <span class="pill">修订 ${p.revision_no}</span>
            ${p.review_status === "reviewed" ? '<span class="pill green">已人工审核</span>' : '<span class="pill amber">未审核</span>'}
            <span class="muted" style="font-size:12.5px">源 v${p.source_version_no}</span></div>
          <div class="row" style="gap:6px;flex-wrap:wrap">
            <button class="btn ghost sm" data-pa="view" data-pid="${p.id}" data-vid="${versionId}">查看</button>
            <button class="btn ghost sm" data-pa="edit" data-pid="${p.id}" data-vid="${versionId}">编辑</button>
            <button class="btn ghost sm" data-pa="review" data-pid="${p.id}" data-vid="${versionId}">${p.review_status === "reviewed" ? "取消审核" : "标记已审核"}</button>
            <button class="btn ghost sm" data-pa="copy" data-pid="${p.id}">复制</button>
            <button class="btn ghost sm" data-pa="exp" data-pid="${p.id}">导出TXT</button>
            <button class="btn ghost sm" data-pa="del" data-pid="${p.id}" data-vid="${versionId}">删除</button>
          </div>
        </div>
        <div class="copy-ver-body" id="plat_body_${p.id}" style="display:none"></div>
        <div id="plat_edit_${p.id}" style="display:none"></div>
      </div>`).join("");
    el.querySelectorAll("button[data-pa]").forEach((b) => {
      const pid = b.dataset.pid, vid = b.dataset.vid, a = b.dataset.pa;
      if (a === "view") b.addEventListener("click", () => togglePlatformView(pid, items));
      else if (a === "edit") b.addEventListener("click", () => showPlatformEdit(pid, vid, items));
      else if (a === "review") b.addEventListener("click", () => reviewPlatform(pid, vid, items));
      else if (a === "copy") b.addEventListener("click", () => copyPlatformText(pid));
      else if (a === "exp") b.addEventListener("click", () => exportPlatform(pid));
      else if (a === "del") b.addEventListener("click", () => deletePlatform(pid, vid));
    });
  } catch (e) { el.innerHTML = '<p class="muted">加载失败：' + esc(e.message || e) + '</p>'; }
}

function togglePlatformView(pid, items) {
  const p = items.find((x) => x.id === pid);
  const body = $("plat_body_" + pid);
  if (!p || !body) return;
  if (body.style.display === "none") {
    body.style.display = "";
    body.innerHTML = `
      <div><b>${esc(p.title)}</b></div>
      <div style="white-space:pre-line">${esc(p.body)}</div>
      <div class="muted" style="font-size:12.5px">${esc((p.hashtags || []).map((t) => "#" + t).join(" "))}</div>
      <div class="muted" style="font-size:12.5px">封面：${esc(p.cover_text || "—")} · 置顶：${esc(p.pinned_comment || "—")} · 行动引导：${esc(p.cta || "—")}</div>
      ${p.changes_summary ? `<div class="muted" style="font-size:12.5px;white-space:pre-line">调整说明：\n${esc(p.changes_summary)}</div>` : ""}`;
  } else body.style.display = "none";
}

function showPlatformForm(versionId, useAi, preset) {
  const form = $("plat_form_" + versionId);
  if (!form) return;
  const p = preset || {};
  form.innerHTML = `
    <div class="card" style="margin:8px 0">
      <h4>${useAi ? "AI 将按平台调性改写" : "手动填写平台版本"}（${esc(platName($("plat_sel_" + versionId).value))}）</h4>
      ${useAi ? "" : `
      <label class="fld"><span>标题</span><input id="pf_title_${versionId}" class="input" value="${esc(p.title || "")}"></label>
      <label class="fld"><span>正文</span><textarea id="pf_body_${versionId}" class="input" rows="4">${esc(p.body || "")}</textarea></label>
      <label class="fld"><span>话题（空格分隔，不用加#）</span><input id="pf_tags_${versionId}" class="input" value="${esc((p.hashtags || []).join(" "))}"></label>
      <label class="fld"><span>封面文案</span><input id="pf_cover_${versionId}" class="input" value="${esc(p.cover_text || "")}"></label>
      <label class="fld"><span>置顶评论建议</span><input id="pf_pin_${versionId}" class="input" value="${esc(p.pinned_comment || "")}"></label>
      <label class="fld"><span>行动引导</span><input id="pf_cta_${versionId}" class="input" value="${esc(p.cta || "")}"></label>`}
      <div class="row" style="gap:8px;margin-top:8px">
        <button class="btn primary sm" id="pf_save_${versionId}">${useAi ? "开始生成" : "保存版本"}</button>
        <button class="btn ghost sm" id="pf_cancel_${versionId}">取消</button>
      </div>
      ${useAi ? '<p class="muted" style="font-size:12.5px">AI 只调整表达与结构，不添加新事实主张；结果保存后仍需你人工审核</p>' : ""}
    </div>`;
  $("pf_cancel_" + versionId).addEventListener("click", () => { form.innerHTML = ""; });
  $("pf_save_" + versionId).addEventListener("click", () => genPlatformVersion(versionId, useAi));
}

async function genPlatformVersion(versionId, useAi) {
  const platform = $("plat_sel_" + versionId).value;
  const body = { copy_version_id: versionId, platform, use_ai: useAi };
  if (!useAi) {
    body.title = $("pf_title_" + versionId).value.trim();
    body.body = $("pf_body_" + versionId).value.trim();
    body.hashtags = $("pf_tags_" + versionId).value.trim().split(/\s+/).filter(Boolean);
    body.cover_text = $("pf_cover_" + versionId).value.trim();
    body.pinned_comment = $("pf_pin_" + versionId).value.trim();
    body.cta = $("pf_cta_" + versionId).value.trim();
    if (!body.title || !body.body) { toast("手动模式请填写标题和正文"); return; }
  }
  const btn = $("pf_save_" + versionId);
  btn.disabled = true; btn.textContent = useAi ? "AI 生成中…" : "保存中…";
  try {
    const r = V1(await ApiClient.post(
      `/api/v1/platforms/copy/docs/${COPY.doc.id}/platforms?org_id=` + encodeURIComponent(S.org.id),
      body, { idempotencyKey: "auto" }));
    $("plat_form_" + versionId).innerHTML = "";
    await loadPlatformList(versionId);
    toast(`${platName(platform)}版本已保存（修订 ${r.platform_version.revision_no}），请人工审核`);
    if (r.review_reminder) toast(r.review_reminder, 6000);
  } catch (e) { toast("生成失败：" + (e.message || e)); }
  finally { btn.disabled = false; btn.textContent = useAi ? "开始生成" : "保存版本"; }
}

function showPlatformEdit(pid, versionId, items) {
  const p = items.find((x) => x.id === pid);
  const box = $("plat_edit_" + pid);
  if (!p || !box) return;
  box.style.display = "";
  box.innerHTML = `
    <div class="card" style="margin:8px 0">
      <h4>编辑${esc(platName(p.platform))}版本（只改本版本，其他平台不受影响）</h4>
      <label class="fld"><span>标题</span><input id="pe_title_${pid}" class="input" value="${esc(p.title)}"></label>
      <label class="fld"><span>正文</span><textarea id="pe_body_${pid}" class="input" rows="4">${esc(p.body)}</textarea></label>
      <label class="fld"><span>话题（空格分隔）</span><input id="pe_tags_${pid}" class="input" value="${esc((p.hashtags || []).join(" "))}"></label>
      <label class="fld"><span>封面文案</span><input id="pe_cover_${pid}" class="input" value="${esc(p.cover_text || "")}"></label>
      <label class="fld"><span>置顶评论建议</span><input id="pe_pin_${pid}" class="input" value="${esc(p.pinned_comment || "")}"></label>
      <label class="fld"><span>行动引导</span><input id="pe_cta_${pid}" class="input" value="${esc(p.cta || "")}"></label>
      <div class="row" style="gap:8px;margin-top:8px">
        <button class="btn primary sm" id="pe_save_${pid}">保存</button>
        <button class="btn ghost sm" id="pe_cancel_${pid}">取消</button>
      </div>
    </div>`;
  $("pe_cancel_" + pid).addEventListener("click", () => { box.style.display = "none"; });
  $("pe_save_" + pid).addEventListener("click", async () => {
    try {
      await ApiClient.patch(`/api/v1/platforms/${pid}?org_id=` + encodeURIComponent(S.org.id), {
        title: $("pe_title_" + pid).value,
        body: $("pe_body_" + pid).value,
        hashtags: $("pe_tags_" + pid).value.trim().split(/\s+/).filter(Boolean),
        cover_text: $("pe_cover_" + pid).value,
        pinned_comment: $("pe_pin_" + pid).value,
        cta: $("pe_cta_" + pid).value,
      });
      toast("已保存");
      await loadPlatformList(versionId);
    } catch (e) { toast("保存失败：" + (e.message || e)); }
  });
}

async function reviewPlatform(pid, versionId, items) {
  const p = items.find((x) => x.id === pid);
  const reviewed = p && p.review_status !== "reviewed";
  if (reviewed && !(await modalConfirm({ title: "人工审核确认", body: "确认该平台版本已人工审核无误？", okText: "确认" }))) return;
  try {
    await ApiClient.post(`/api/v1/platforms/${pid}/review?org_id=` + encodeURIComponent(S.org.id), { reviewed });
    toast(reviewed ? "已标记为人工审核" : "已取消审核标记");
    await loadPlatformList(versionId);
    if (reviewed) stampPop(document.querySelector(`[data-pid="${pid}"]`)?.closest(".copy-ver"), "已审核");
  } catch (e) { toast("操作失败：" + (e.message || e)); }
}

async function copyPlatformText(pid) {
  try {
    const r = V1(await ApiClient.get(`/api/v1/platforms/${pid}/export?org_id=` + encodeURIComponent(S.org.id)));
    await navigator.clipboard.writeText(r.text);
    toast("发布文本已复制到剪贴板");
  } catch (e) { toast("复制失败：" + (e.message || e)); }
}

async function exportPlatform(pid) {
  try {
    const r = V1(await ApiClient.get(`/api/v1/platforms/${pid}/export?org_id=` + encodeURIComponent(S.org.id)));
    const blob = new Blob([r.text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `平台发布_${r.platform_name}_${new Date().toISOString().slice(0, 10)}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("已导出 TXT");
  } catch (e) { toast("导出失败：" + (e.message || e)); }
}

async function deletePlatform(pid, versionId) {
  if (!(await modalConfirm({ title: "删除平台版本", body: "删除这个平台版本？不影响其他平台版本。\n此操作不可恢复。", okText: "删除", danger: true }))) return;
  try {
    await ApiClient.del(`/api/v1/platforms/${pid}?org_id=` + encodeURIComponent(S.org.id));
    toast("已删除");
    await loadPlatformList(versionId);
  } catch (e) { toast("删除失败：" + (e.message || e)); }
}




/* ================= V3.0 C：内容策划工作台（v3 创意页扩展，零 HTML 改动） ================= */
const PLAN_GOALS = [["brand", "品牌宣传"], ["sales", "商品销售"], ["intro", "产品介绍"], ["record", "产地记录"]];
const PLAN = { taskId: null, task: null, candidates: [], selectedIndex: null, busy: false, feas: null };

function planGoalLabel(g) { const f = PLAN_GOALS.find((x) => x[0] === g); return f ? f[1] : g; }

/** 注入内容策划面板到 v3 视图顶部（在创意方案卡片之前）。 */
function initPlanSection() {
  if ($("plan_panel") || !$("v3")) return;
  const v3 = $("v3");
  const anchor = v3.querySelector(".card.panel");
  const panel = document.createElement("div");
  panel.className = "card panel";
  panel.id = "plan_panel";
  panel.style.marginBottom = "18px";
  if (anchor) v3.insertBefore(panel, anchor);
  else v3.prepend(panel);
}

function renderPlanSection() {
  const host = $("plan_panel");
  if (!host) return;
  if (!S.org) { host.innerHTML = "<h3>内容策划</h3><p style='font-size:13.5px;color:var(--muted)'>请先登录并选择组织</p>"; return; }
  const t = PLAN.task || {};
  const goal = t.content_goal || PLAN._goal || "brand";
  const prods = S.products || [];
  const pid = t.product_id || "";
  host.innerHTML = `
    <h3>内容策划 <span class="pill gray">${PLAN.taskId ? "任务：" + esc(t.title || "") : "从营销目标到创意方向"}</span></h3>
    <p style="font-size:12.5px;color:var(--muted);margin:6px 0 12px">先定目标、受众和实拍条件 → 生成 3-5 个候选创意 → 选中后关联拍摄项目，继续走分镜/实拍流程（不重复生成执行单）</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      ${PLAN_GOALS.map((g) => `<button class="pill ${g[0] === goal ? "blue" : ""}" data-plan-goal="${g[0]}" style="cursor:pointer">${g[1]}</button>`).join("")}
    </div>
    <div class="form-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
      <label class="f">目标受众<input id="plan_audience" class="input sm" value="${esc(t.audience || "")}" placeholder="如：25-40 岁城市女性"></label>
      <label class="f">内容目的<input id="plan_purpose" class="input sm" value="${esc(t.content_purpose || "")}" placeholder="如：提升品牌认知"></label>
      <label class="f" style="grid-column:1/-1">核心信息<input id="plan_core" class="input sm" value="${esc(t.core_message || "")}" placeholder="一句话说清这条内容要传达什么"></label>
      <label class="f">已确认卖点（逗号分隔）<input id="plan_selling" class="input sm" value="${esc((t.selling_points || []).join("，"))}" placeholder="如：果大，汁多，当季鲜"></label>
      <label class="f">创作风格<input id="plan_style" class="input sm" value="${esc(t.style || "")}" placeholder="如：实在不端着（不填按目标自然选择）"></label>
      <label class="f">期望时长（秒）<input id="plan_duration" class="input sm" type="number" min="5" value="${t.target_duration_seconds || 30}"></label>
      <label class="f">目标平台（逗号分隔）<input id="plan_platforms" class="input sm" value="${esc((t.target_platforms || []).join("，"))}" placeholder="如：抖音，快手"></label>
      <label class="f">关联产品档案（可选）
        <select id="plan_product" class="input sm">
          <option value="">不关联</option>
          ${prods.map((p) => `<option value="${esc(p.id)}"${p.id === pid ? " selected" : ""}>${esc(p.name)}（${esc(p.code)}）</option>`).join("")}
        </select></label>
      <label class="f" style="grid-column:1/-1">实拍条件<input id="plan_shoot" class="input sm" value="${esc(planShootText(t))}" placeholder="如：手机拍摄/1人/自然光/果园"></label>
      <label class="f" style="grid-column:1/-1">宣传限制<input id="plan_restrict" class="input sm" value="${esc(t.ad_restrictions || "")}" placeholder="如：不宣称医疗功效"></label>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <button class="btn primary sm" id="plan_gen_btn" ${PLAN.busy ? "disabled" : ""}>${PLAN.taskId ? "重新生成候选" : "✦ 生成候选创意"}</button>
      ${PLAN.taskId ? `<button class="btn ghost sm" id="plan_new_btn">新建策划任务</button>` : ""}
      <span class="err-text" id="plan_err"></span>
    </div>
    <div id="plan_candidates" style="margin-top:6px"></div>
    ${PLAN._err ? `<div class="err-text" style="display:block;margin-top:8px">⚠ ${esc(PLAN._err)}</div>` : ""}
    <div id="plan_after" style="margin-top:10px"></div>`;
  host.querySelectorAll("[data-plan-goal]").forEach((b) => b.addEventListener("click", () => {
    if (!PLAN.task) { PLAN._goal = b.dataset.planGoal; renderPlanSection(); return; }
    planUpdate({ content_goal: b.dataset.planGoal });
  }));
  $("plan_gen_btn").addEventListener("click", planGenerate);
  const nb = $("plan_new_btn");
  if (nb) nb.addEventListener("click", () => { PLAN.taskId = null; PLAN.task = null; PLAN.candidates = []; PLAN.selectedIndex = null; PLAN.feas = null; renderPlanSection(); });
  renderPlanCandidates();
}

/** 实拍条件对象转展示文本。 */
function planShootText(t) {
  const s = (t && t.shoot_conditions) || {};
  const parts = [s.device, s.people ? s.people + "人" : "", s.lighting, s.location].filter(Boolean);
  return parts.join("/") || "";
}

/** 从表单收集策划输入。 */
function collectPlanInput() {
  const v = (id) => { const el = $(id); return el ? el.value.trim() : ""; };
  const shootTxt = v("plan_shoot");
  const shoot = {};
  if (shootTxt) {
    const parts = shootTxt.split("/");
    shoot.device = (parts[0] || "").trim();
    shoot.people = (parts[1] || "").trim();
    shoot.lighting = (parts[2] || "").trim();
    shoot.location = (parts[3] || "").trim();
  }
  return {
    title: "策划-" + planGoalLabel(PLAN.task ? PLAN.task.content_goal : (PLAN._goal || "brand")) + "-" + new Date().toLocaleDateString(),
    content_goal: PLAN.task ? PLAN.task.content_goal : (PLAN._goal || "brand"),
    product_id: v("plan_product") || null,
    audience: v("plan_audience"),
    content_purpose: v("plan_purpose"),
    core_message: v("plan_core"),
    selling_points: v("plan_selling").split(/[，,]/).map((x) => x.trim()).filter(Boolean),
    style: v("plan_style"),
    target_duration_seconds: parseInt(v("plan_duration"), 10) || 30,
    target_platforms: v("plan_platforms").split(/[，,]/).map((x) => x.trim()).filter(Boolean),
    shoot_conditions: shoot,
    ad_restrictions: v("plan_restrict"),
  };
}

async function planGenerate() {
  if (PLAN.busy || !S.org) return;
  PLAN.busy = true; renderPlanSection();
  errText("plan_err", "");
  aiLoading("正在生成候选创意…");
  try {
    const input = collectPlanInput();
    let taskId = PLAN.taskId;
    if (!taskId) {
      const created = V1(await ApiClient.post("/api/v1/plan/tasks?org_id=" + encodeURIComponent(S.org.id), input));
      taskId = created.id;
    } else {
      await ApiClient.patch("/api/v1/plan/tasks/" + taskId + "?org_id=" + encodeURIComponent(S.org.id), input);
    }
    const d = V1(await ApiClient.post(
      "/api/v1/plan/tasks/" + taskId + "/generate?org_id=" + encodeURIComponent(S.org.id),
      {}, { idempotencyKey: "auto" }));
    PLAN.taskId = taskId; PLAN.task = d; PLAN.candidates = d.candidates || [];
    PLAN.selectedIndex = d.selected_index; PLAN.feas = null; PLAN._err = null;
    if (d.product_facts_used) toast("已带入 " + d.product_facts_used.length + " 条已确认产品事实");
    toast("已生成 " + PLAN.candidates.length + " 个候选创意");
  } catch (e) {
    PLAN._err = e.message || "生成失败";
  } finally {
    PLAN.busy = false; aiLoadingDone(); renderPlanSection();
  }
}

async function planUpdate(fields) {
  if (!PLAN.taskId || !S.org) return;
  try {
    const d = V1(await ApiClient.patch(
      "/api/v1/plan/tasks/" + PLAN.taskId + "?org_id=" + encodeURIComponent(S.org.id), fields));
    PLAN.task = d; renderPlanSection();
  } catch (e) { toast(e.message || "更新失败"); }
}

function renderPlanCandidates() {
  const host = $("plan_candidates");
  if (!host) return;
  const cands = PLAN.candidates || [];
  if (!cands.length) { host.innerHTML = ""; renderPlanAfter(); return; }
  host.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">` +
    cands.map((c, i) => `
      <div class="idea-card ${i === PLAN.selectedIndex ? "sel" : ""}" data-i="${i}" style="cursor:pointer" tabindex="0" role="button">
        <b>方向 ${i + 1} · ${esc(c.name || "")}</b>
        <p>${esc(c.core_expression || "")}</p>
        <div style="margin:8px 0;display:flex;gap:6px;flex-wrap:wrap">
          <span class="pill blue">${esc(planGoalLabel(c.goal || ""))}</span>
          <span class="pill ${c.difficulty === "高" ? "amber" : "gray"}">难度${esc(c.difficulty || "未知")}</span>
          ${(c.fact_refs || []).length ? `<span class="pill gray">事实引用 ${c.fact_refs.length}</span>` : ""}
        </div>
        <div class="kv"><b>开头切入</b><span>${esc(c.hook_opening || "—")}</span></div>
        <div class="kv"><b>内容结构</b><span>${esc(c.structure || "—")}</span></div>
        <div class="kv"><b>建议画面</b><span>${esc(c.suggested_shots || "—")}</span></div>
        ${c.shoot_notes ? `<div class="kv"><b>执行注意</b><span>${esc(c.shoot_notes)}</span></div>` : ""}
      </div>`).join("") + `</div>`;
  host.querySelectorAll(".idea-card").forEach((el) => { keyActivate(el); el.addEventListener("click", () => planSelect(+el.dataset.i)); });
  renderPlanAfter();
}

async function planSelect(i) {
  if (!PLAN.taskId || !S.org) return;
  try {
    const d = V1(await ApiClient.post(
      "/api/v1/plan/tasks/" + PLAN.taskId + "/select?org_id=" + encodeURIComponent(S.org.id),
      { index: i }));
    PLAN.task = d; PLAN.selectedIndex = d.selected_index; PLAN.feas = null;
    toast("已选择方向 " + (i + 1));
    renderPlanCandidates();
    planFeasibility(); // 选中后自动跑纯规则可行性检查
  } catch (e) { toast(e.message || "选择失败"); }
}

async function planFeasibility() {
  if (!PLAN.taskId || PLAN.selectedIndex == null || !S.org) return;
  try {
    const d = V1(await ApiClient.post(
      "/api/v1/plan/tasks/" + PLAN.taskId + "/feasibility?org_id=" + encodeURIComponent(S.org.id), {}));
    PLAN.feas = d;
  } catch (e) { PLAN.feas = { status: "error", message: e.message }; }
  renderPlanAfter();
}

function renderPlanAfter() {
  const host = $("plan_after");
  if (!host) return;
  if (PLAN.selectedIndex == null) { host.innerHTML = ""; return; }
  const f = PLAN.feas;
  let feasHtml = "<p style='font-size:12.5px;color:var(--muted)'>正在检查实拍可行性…</p>";
  if (f) {
    if (f.status === "error") {
      feasHtml = `<p style="font-size:12.5px;color:var(--danger)">可行性检查失败：${esc(f.message || "")}</p>`;
    } else {
      const pill = f.status === "ready" ? "green" : "amber";
      const label = f.status === "ready" ? "可执行" : (f.status === "need_confirm" ? "需确认" : "建议调整");
      feasHtml = `<div style="margin:8px 0"><span class="pill ${pill}">可行性：${label}</span>
        <span style="font-size:12.5px;color:var(--muted)">（纯规则检查，不消耗 AI 配额）</span></div>` +
        (f.checks || []).map((c) => `<div style="font-size:12.5px;margin:4px 0">· ${esc(c.item || "")}：${esc(c.message || "")}${c.alternative ? `<br><span style="color:var(--muted)">替代方案：${esc(c.alternative)}</span>` : ""}</div>`).join("");
    }
  }
  const works = (S.works || []).filter((w) => w.status !== "archived");
  host.innerHTML = `
    <div class="card" style="padding:12px;background:var(--bg-soft)">
      <b style="font-size:13.5px">已选方向 ${PLAN.selectedIndex + 1} · 下一步</b>
      ${feasHtml}
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
        <select id="plan_work_select" class="input sm" style="min-width:200px">
          <option value="">选择已有拍摄项目…</option>
          ${works.map((w) => `<option value="${esc(w.id)}">${esc(w.title || "未命名项目")}</option>`).join("")}
        </select>
        <button class="btn primary sm" id="plan_link_btn">关联拍摄项目</button>
        <button class="btn ghost sm" id="plan_new_work_btn">新建拍摄项目</button>
        <span style="font-size:12.5px;color:var(--muted)">关联后继续走分镜/实拍流程，不重复生成执行单</span>
      </div>
      <div class="err-text" id="plan_link_err"></div>
    </div>`;
  $("plan_link_btn").addEventListener("click", () => planLinkWork(false));
  $("plan_new_work_btn").addEventListener("click", () => planLinkWork(true));
}

async function planLinkWork(createNew) {
  if (!PLAN.taskId || !S.org) return;
  errText("plan_link_err", "");
  const body = { create_new: !!createNew };
  if (!createNew) {
    const wid = $("plan_work_select").value;
    if (!wid) { errText("plan_link_err", "请先选择一个拍摄项目"); return; }
    body.work_id = wid;
  } else {
    body.title = (PLAN.task && PLAN.task.title ? PLAN.task.title : "策划项目") + "-拍摄";
  }
  try {
    const d = V1(await ApiClient.post(
      "/api/v1/plan/tasks/" + PLAN.taskId + "/link-work?org_id=" + encodeURIComponent(S.org.id), body));
    PLAN.task = d;
    await loadWorks();
    const w = (S.works || []).find((x) => x.id === d.linked_work_id);
    if (w) {
      S.activeWork = w;
      S.data = blankData();
      Object.assign(S.data, JSON.parse(JSON.stringify(w.data || {})));
    }
    toast(createNew ? "已新建拍摄项目，创意方向已带入" : "已关联拍摄项目，创意方向已带入");
    go("v3");
  } catch (e) { errText("plan_link_err", e.message || "关联失败"); }
}
/* ================= V3.0 F：发布中心（独立 v8 视图，零 HTML 改动） ================= */
const PUB_STATUSES = [
  ["pending", "待准备"], ["review", "待审核"], ["ready", "待发布"],
  ["published", "已发布"], ["reviewed", "已复盘"],
  ["failed", "失败"], ["postponed", "暂缓"], ["cancelled", "取消"],
];
const PUB_TRANSITIONS = {
  pending: ["review", "cancelled"],
  review: ["ready", "pending", "cancelled"],
  ready: ["published", "postponed", "cancelled"],
  published: ["reviewed", "failed"],
  reviewed: [], failed: ["pending"], postponed: ["pending"], cancelled: [],
};
const PUB = { records: [], rec: null, filter: "", works: [], busy: false };

function pubStatusLabel(s) { const f = PUB_STATUSES.find((x) => x[0] === s); return f ? f[1] : s; }
const PUB_STATUS_PILL = { pending: "amber", review: "blue", ready: "green", reviewed: "blue", failed: "red", postponed: "gray", cancelled: "gray" };
function pubPlatformLabel(p) {
  return { douyin: "抖音", kuaishou: "快手", xiaohongshu: "小红书", shipinhao: "视频号" }[p] || p || "未指定";
}

function initPublishView() {
  if (!document.querySelector('.nav-item[data-v="v8"]')) {
    const navV7 = document.querySelector('.nav-item[data-v="v7"]');
    const b = document.createElement("button");
    b.className = "nav-item"; b.dataset.v = "v8";
    b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 12v7a1 1 0 001 1h14a1 1 0 001-1v-7M12 3v13M7 8l5-5 5 5"/></svg>发布中心';
    b.addEventListener("click", () => go("v8"));
    if (navV7) navV7.after(b); else document.querySelector(".nav")?.appendChild(b);
  }
  if (!$("v8")) {
    const anchor = $("v7") || $("v6");
    if (anchor) {
      const s = document.createElement("section");
      s.className = "view"; s.id = "v8";
      anchor.after(s);
    }
  }
}

function pubApi(path, opts) {
  const q = "?org_id=" + encodeURIComponent(S.org.id);
  const url = "/api/v1/publish" + path + q;
  const method = (opts && opts.method) || "GET";
  const body = (opts && opts.body) || {};
  if (method === "POST") return ApiClient.post(url, body);
  if (method === "PATCH") return ApiClient.patch(url, body);
  if (method === "DELETE") return ApiClient.del(url);
  return ApiClient.get(url);
}

async function loadPublishRecords() {
  try {
    const r = V1(await pubApi("/records" + (PUB.filter ? "&status=" + PUB.filter : "")));
    PUB.records = r || [];
  } catch (e) { PUB.records = []; }
}

async function renderPublishCenter() {
  const sec = $("v8");
  if (!sec) return;
  if (!S.org) { sec.innerHTML = '<div class="page-head"><h2>发布中心</h2><p class="muted">请先登录并选择组织</p><div class="page-meander" aria-hidden="true"></div></div>'; return; }
  initPublishView();
  sec.innerHTML = viewSkeleton(); // 先给加载态：线上网关延迟下不能留白
  // V3.7：作品列表与发布记录并行拉取（按作品归拢分组需要作品标题）
  await Promise.all([
    loadPublishRecords(),
    ApiClient.get("/api/v1/works?org_id=" + encodeURIComponent(S.org.id))
      .then((r) => { PUB.works = V1(r) || []; })
      .catch(() => { /* 保留旧列表，分组降级为未关联 */ }),
  ]);
  renderPublishList();
}

/* V3.7：发布记录按源作品归拢；未关联作品的记录排最后 */
function pubGroupItems() {
  const byId = new Map();
  PUB.works.forEach((w) => byId.set(w.id, { key: w.id, title: w.title || "未命名作品", items: [] }));
  const orphan = [];
  PUB.records.forEach((r) => {
    const g = (r.work_id && byId.get(r.work_id)) || null;
    if (g) g.items.push(r); else orphan.push(r);
  });
  const out = Array.from(byId.values()).filter((g) => g.items.length);
  if (orphan.length) out.push({ key: "_none", title: "未关联作品", items: orphan, unknown: true });
  return out;
}

function renderPublishList() {
  const sec = $("v8");
  const opts = ['<option value="">全部状态</option>'].concat(
    PUB_STATUSES.map(([v, l]) => `<option value="${v}"${PUB.filter === v ? " selected" : ""}>${l}</option>`)).join("");
  // V3.7：按源作品归拢分组（状态筛选结果仍然生效，只改展示方式）
  const groupsHtml = pubGroupItems().map((g) => `
    <div class="wgroup">
      <div class="wgroup-head">
        <h3><span class="wg-name">${esc(g.title)}</span></h3>
        <span class="pill ${g.unknown ? "gray" : "blue"}">${g.items.length} 条记录</span>
        <span class="wg-line"></span>
      </div>
      <div class="pub-list">${g.items.map((r) => `
        <div class="card pub-card" data-id="${esc(r.id)}">
          <div class="pub-card-title" title="${esc(r.title)}">${esc(r.title)}</div>
          <div class="pub-card-meta"><span class="pill gray">${esc(pubPlatformLabel(r.platform))}</span>
            ${r.status === "published" ? '<span class="stamp-badge">已发布</span>' : `<span class="pill ${PUB_STATUS_PILL[r.status] || "gray"}">${esc(pubStatusLabel(r.status))}</span>`}
          </div>
          <div class="muted small pub-card-link">${r.publish_url ? "链接：" + esc(r.publish_url) : "尚未发布，状态流转到「待发布」后填写链接"}</div>
          <div class="pub-card-actions"><button class="btn sm ghost" data-act="open">打开</button>
          ${["pending", "cancelled", "failed", "postponed"].includes(r.status) ? '<button class="btn sm danger" data-act="del">删除</button>' : ""}</div>
        </div>`).join("")}</div>
    </div>`).join("");
  sec.innerHTML = `
    <div class="page-head"><h2>发布中心</h2>
    <p class="muted">为每次发布建立独立记录：发布前检查 → 手动状态流转 → 记录发布时间与链接。本工具仅支持人工复制发布。</p><div class="page-meander" aria-hidden="true"></div></div>
    <div class="toolbar">
      <span class="tl-label">状态筛选</span>
      <select id="pub_filter" class="input" style="flex:0 1 150px">${opts}</select>
      <span class="sep"></span>
      <span class="tl-label">新建发布记录</span>
      <input id="pub_new_title" class="input" placeholder="标题，如：章丘大葱 · 抖音品牌短片" style="flex:2 1 220px">
      <select id="pub_new_platform" class="input" style="flex:0 1 130px">
        <option value="douyin">抖音</option><option value="kuaishou">快手</option>
        <option value="xiaohongshu">小红书</option><option value="shipinhao">视频号</option>
      </select>
      <button class="btn primary" id="pub_new_btn">＋ 新建</button>
    </div>
    <div id="pub_detail"></div>
    ${groupsHtml || '<div class="card pub-empty">' + emptyState("暂无发布记录", "填好标题、选好平台，建第一条发布记录。", true, { label: "新建发布记录", fn: () => { const t = $("pub_new_title"); if (t) { t.focus(); t.scrollIntoView({ block: "center", behavior: "smooth" }); } } }) + '</div>'}`;
  $("pub_filter").addEventListener("change", async (e) => { PUB.filter = e.target.value; await loadPublishRecords(); renderPublishList(); });
  $("pub_new_btn").addEventListener("click", createPublishRecord);
  sec.querySelectorAll(".pub-card").forEach((c) => {
    c.querySelector('[data-act="open"]').addEventListener("click", () => openPublishRecord(c.dataset.id));
    const del = c.querySelector('[data-act="del"]');
    if (del) del.addEventListener("click", () => deletePublishRecord(c.dataset.id));
  });
  if (PUB.rec) renderPublishDetail();
}

async function createPublishRecord() {
  const title = $("pub_new_title").value.trim() || "未命名发布";
  const platform = $("pub_new_platform").value;
  try {
    const d = V1(await pubApi("/records", { method: "POST", body: { title, platform } }));
    PUB.records.unshift(d);
    PUB.rec = d;
    renderPublishList();
    toast("已创建发布记录");
  } catch (e) { toast("创建失败：" + (e.message || e)); }
}

async function deletePublishRecord(id) {
  if (!(await modalConfirm({ title: "删除发布记录", body: "确定删除这条发布记录吗？（只删除记录，不删除已关联的文件）\n此操作不可恢复。", okText: "删除", danger: true }))) return;
  try {
    await pubApi(`/records/${id}`, { method: "DELETE" });
    PUB.records = PUB.records.filter((r) => r.id !== id);
    if (PUB.rec && PUB.rec.id === id) PUB.rec = null;
    renderPublishList();
    toast("已删除");
  } catch (e) { toast("删除失败：" + (e.message || e)); }
}

async function openPublishRecord(id) {
  try {
    PUB.rec = V1(await pubApi(`/records/${id}`));
    renderPublishDetail();
    $("pub_detail")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast("打开失败：" + (e.message || e)); }
}

function renderPublishDetail() {
  const box = $("pub_detail");
  const r = PUB.rec;
  if (!box || !r) return;
  const allowed = PUB_TRANSITIONS[r.status] || [];
  const transBtns = allowed.map((s) => `<button class="btn sm" data-to="${s}">→ ${pubStatusLabel(s)}</button>`).join("");
  const checks = (r.check_result || []).map((i) => `
    <div class="check-item ${i.passed ? "ok" : "fail"}">
      <span>${i.passed ? "✓" : "✗"}</span>
      <span><strong>${esc(i.label)}</strong>${i.blocking ? "（必须）" : "（参考）"}</span>
      <span class="muted">${esc(i.detail || "")}</span>
    </div>`).join("");
  box.innerHTML = `
    <div class="card pub-detail">
      <div class="pub-card-head"><h3>${esc(r.title)}</h3>
        <span class="pill">${esc(pubPlatformLabel(r.platform))}</span>
        ${r.status === "published" ? '<span class="stamp-badge">已发布</span>' : `<span class="pill">${esc(pubStatusLabel(r.status))}</span>`}</div>
      <div class="grid2 small">
        <div>源拍摄项目：${esc(r.work_id || "未关联")}</div>
        <div>文案版本：${esc(r.copy_version_id || "未关联")}</div>
        <div>平台版本：${esc(r.platform_version_id || "未关联")}</div>
        <div>视频文件：${esc(r.video_asset_id || "未关联")}</div>
        <div>授权确认：${r.authorization_confirmed ? "已确认" : '<span class="warn">未确认</span>'}
          ${r.status === "pending" || r.status === "review" ? '<button class="btn sm" id="pub_auth_btn">确认授权</button>' : ""}</div>
        <div>发布时间：${esc(r.published_at || "—")}</div>
      </div>
      <div class="row"><button class="btn" id="pub_check_btn">运行发布前检查</button>
        <button class="btn" id="pub_export_btn">复制发布包</button>
        <button class="btn" id="pub_dl_btn">下载发布包</button>
        <button class="btn sm" id="pub_close_btn">收起</button></div>
      <div id="pub_checks">${checks || '<p class="muted">尚未运行发布前检查</p>'}</div>
      ${r.checked_at ? `<div class="muted small">检查时间：${esc(r.checked_at)}</div>` : ""}
      <div class="row" style="margin-top:8px"><span class="muted">状态流转：</span>${transBtns || '<span class="muted">终态，无可用操作</span>'}</div>
      <div class="row" id="pub_pubinfo" style="${r.status === "ready" ? "" : "display:none"}">
        <input id="pub_at" class="input" placeholder="发布时间，如 2026-10-20T18:00:00+08:00" value="${esc(r.published_at || "")}">
        <input id="pub_url" class="input" placeholder="发布链接" style="flex:1" value="${esc(r.publish_url || "")}">
      </div>
      <div class="row"><input id="pub_notes" class="input" placeholder="备注 / 复盘记录" style="flex:1" value="${esc(r.notes || "")}">
        <button class="btn sm" id="pub_notes_btn">保存备注</button></div>
    </div>`;
  $("pub_check_btn").addEventListener("click", () => runPublishCheck(r.id));
  $("pub_export_btn").addEventListener("click", () => copyPublishPackage(r.id));
  $("pub_dl_btn").addEventListener("click", () => downloadPublishPackage(r.id));
  $("pub_close_btn").addEventListener("click", () => { PUB.rec = null; renderPublishList(); });
  $("pub_notes_btn").addEventListener("click", () => savePublishNotes(r.id));
  const authBtn = $("pub_auth_btn");
  if (authBtn) authBtn.addEventListener("click", () => setPublishAuth(r.id));
  box.querySelectorAll("[data-to]").forEach((b) => b.addEventListener("click", () => transitionPublish(r.id, b.dataset.to)));
  if (r.status === "published" || r.status === "reviewed") renderReviewPanel(r.id);
}

// ============ V3.0 H：效果复盘（注入 v8 发布中心） ============

const REV = { review: null, busy: false };
const METRIC_FIELD_LABELS = [
  ["plays", "播放量"], ["likes", "点赞"], ["comments", "评论"],
  ["shares", "转发"], ["favorites", "收藏"], ["followers_gain", "粉丝增长"],
];

function reviewApi(path, opts) {
  const q = "?org_id=" + encodeURIComponent(S.org.id);
  const url = "/api/v1/review" + path + q;
  const method = (opts && opts.method) || "GET";
  const body = (opts && opts.body) || {};
  if (method === "POST") return ApiClient.post(url, body);
  if (method === "PATCH") return ApiClient.patch(url, body);
  if (method === "DELETE") return ApiClient.del(url);
  return ApiClient.get(url);
}

function revSourceLabel(s) { return s === "screenshot" ? "平台截图导入" : "人工录入"; }

async function renderReviewPanel(recId) {
  let box = $("rev_panel");
  if (!box) {
    const card = document.querySelector(".pub-detail");
    if (!card) return;
    box = document.createElement("div");
    box.id = "rev_panel";
    box.className = "rev-panel";
    card.appendChild(box);
  }
  box.innerHTML = '<p class="muted">加载复盘数据…</p>';
  try {
    const r = V1(await reviewApi(`/records/${recId}/review`));
    REV.review = r;
  } catch (e) { REV.review = null; }
  // 竞态保护：await 期间用户可能已切走/列表重渲染，面板 detached 则直接放弃
  box = $("rev_panel");
  if (!box || !box.isConnected) return;
  const r = REV.review;
  if (!r) { box.innerHTML = '<p class="warn">复盘数据加载失败</p>'; return; }
  const metricFields = METRIC_FIELD_LABELS.map(([k, l]) =>
    `<label class="rev-field">${l}<input id="rev_m_${k}" class="input" type="number" min="0" placeholder="0"></label>`).join("");
  const hist = (r.metrics || []).map((m) => {
    const pairs = Object.entries(m.metrics || {}).map(([k, v]) => `${esc(k)}=${esc(String(v))}`).join("、");
    const rng = (m.date_start || m.date_end) ? `${esc(m.date_start || "?")} ~ ${esc(m.date_end || "?")}` : "未标注时间范围";
    return `<div class="rev-hist-item"><strong>${esc(revSourceLabel(m.source))}</strong>
      <span class="muted small">${rng} · 录入于 ${esc((m.recorded_at || "").slice(0, 16).replace("T", " "))}</span>
      <div>${pairs || "（无指标值）"}</div>${m.note ? `<div class="muted small">备注：${esc(m.note)}</div>` : ""}</div>`;
  }).join("");
  const sugs = (r.suggestions || []).map((s) => {
    const st = { pending: "待处理", adopted: "已采纳", dismissed: "已忽略" }[s.status] || s.status;
    return `<div class="rev-sug"><div class="row"><span class="pill">${st}</span>
      <span class="muted small">${esc((s.created_at || "").slice(0, 16).replace("T", " "))}</span></div>
      <div class="rev-sug-text">${esc(s.suggestion_text).replace(/\n/g, "<br>")}</div>
      ${s.status === "pending" ? `<div class="row"><button class="btn sm" data-sug="${esc(s.id)}" data-dec="adopted">采纳</button>
        <button class="btn sm" data-sug="${esc(s.id)}" data-dec="dismissed">忽略</button></div>` : ""}</div>`;
  }).join("");
  box.innerHTML = `
    <hr><h4>效果复盘</h4>
    <p class="muted small">所有数据均为人工录入，系统不编造任何指标；无数据时此处保持空白。</p>
    <div class="rev-form">
      <div class="row rev-grid">${metricFields}</div>
      <div class="row">
        <select id="rev_m_source" class="input"><option value="manual">人工录入</option><option value="screenshot">平台截图导入</option></select>
        <input id="rev_m_ds" class="input" type="date" title="数据开始日期">
        <input id="rev_m_de" class="input" type="date" title="数据结束日期">
      </div>
      <div class="row"><input id="rev_m_note" class="input" placeholder="备注（可选）" style="flex:1">
        <button class="btn primary sm" id="rev_m_add">录入指标</button></div>
    </div>
    <h5>指标历史 ${r.has_data ? "" : '<span class="muted">（暂无数据）</span>'}</h5>
    <div id="rev_hist">${hist || '<p class="muted">暂无数据：还没有录入任何效果指标。</p>'}</div>
    <h5>复盘结论（人工）</h5>
    <textarea id="rev_note" class="input" rows="3" placeholder="写下这次发布的复盘结论…">${esc(r.review_note || "")}</textarea>
    <div class="row"><button class="btn sm" id="rev_note_btn">保存复盘结论</button>
      <button class="btn sm" id="rev_suggest_btn">AI 给改进建议</button></div>
    <div id="rev_sugs" style="margin-top:8px">${sugs || '<p class="muted small">暂无 AI 建议。建议仅供参考，采纳前请人工确认，不会自动修改任何已确认内容。</p>'}</div>`;
  $("rev_m_add").addEventListener("click", () => addEffectMetric(recId));
  $("rev_note_btn").addEventListener("click", () => saveReviewNote(recId));
  $("rev_suggest_btn").addEventListener("click", () => requestReviewSuggest(recId));
  box.querySelectorAll("[data-sug]").forEach((b) => b.addEventListener("click", () => decideSuggestion(recId, b.dataset.sug, b.dataset.dec)));
}

async function addEffectMetric(recId) {
  const metrics = {};
  METRIC_FIELD_LABELS.forEach(([k]) => {
    const v = $("rev_m_" + k)?.value;
    if (v !== "" && v !== undefined && v !== null) metrics[k] = Number(v);
  });
  if (!Object.keys(metrics).length) { toast("请至少填写一项指标"); return; }
  try {
    await reviewApi(`/records/${recId}/metrics`, { method: "POST", body: {
      source: $("rev_m_source").value,
      date_start: $("rev_m_ds").value || null,
      date_end: $("rev_m_de").value || null,
      metrics, note: $("rev_m_note").value || "",
    } });
    toast("指标已录入");
    renderReviewPanel(recId);
  } catch (e) { toast("录入失败：" + (e.message || e)); }
}

async function saveReviewNote(recId) {
  try {
    await reviewApi(`/records/${recId}/review-note`, { method: "PATCH", body: { review_note: $("rev_note").value } });
    toast("复盘结论已保存");
  } catch (e) { toast("保存失败：" + (e.message || e)); }
}

async function requestReviewSuggest(recId) {
  const btn = $("rev_suggest_btn");
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const r = V1(await reviewApi(`/records/${recId}/suggest`, { method: "POST" }));
    toast("已生成改进建议（仅供参考，不会自动修改任何内容）");
    renderReviewPanel(recId);
  } catch (e) { toast("AI 建议暂不可用：" + (e.message || e)); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "AI 给改进建议"; } }
}

async function decideSuggestion(recId, sugId, dec) {
  try {
    await reviewApi(`/suggestions/${sugId}/decision`, { method: "POST", body: { decision: dec } });
    toast(dec === "adopted" ? "已标记采纳" : "已忽略");
    renderReviewPanel(recId);
  } catch (e) { toast("操作失败：" + (e.message || e)); }
}

// 效果看板（发布中心列表顶部入口）
async function renderReviewDashboard() {
  const sec = $("v8");
  if (!sec) return;
  let box = $("rev_dash");
  if (!box) {
    box = document.createElement("div");
    box.id = "rev_dash";
    box.className = "card";
    const head = sec.querySelector(".page-head");
    if (head) head.after(box); else sec.prepend(box);
  }
  box.innerHTML = '<p class="muted">加载效果看板…</p>';
  try {
    const d = V1(await reviewApi("/dashboard"));
    // 竞态保护：await 期间视图可能已切换
    box = $("rev_dash");
    if (!box || !box.isConnected) return;
    const items = (d.records || []).map((it) => {
      const latest = it.latest;
      const pairs = latest ? Object.entries(latest.metrics || {}).map(([k, v]) => `${esc(k)}=${esc(String(v))}`).join("、") : "";
      return `<div class="rev-dash-item"><strong>${esc(it.title)}</strong>
        <span class="pill">${esc(it.platform || "")}</span>
        ${it.status === "reviewed" ? '<span class="pill">已复盘</span>' : '<span class="stamp-badge">已发布</span>'}
        <div class="muted small">共录入 ${it.batches} 批${it.review_note ? " · 有复盘结论" : ""}</div>
        ${pairs ? `<div>最新：${pairs}</div>` : '<div class="muted">暂无数据</div>'}</div>`;
    }).join("");
    box.innerHTML = `<h4>效果看板</h4>
      ${d.has_data ? `<div class="rev-dash-list">${items}</div>`
        : '<p class="muted">暂无数据：还没有录入任何效果指标，打开一条已发布记录即可人工录入。</p>'}
      <div class="row"><button class="btn sm" id="rev_dash_hide">收起</button></div>`;
    $("rev_dash_hide").addEventListener("click", () => { box.remove(); });
  } catch (e) { box.innerHTML = '<p class="warn">看板加载失败</p>'; }
}

function initReviewDashboardBtn() {
  if (document.querySelector("#rev_dash_btn")) return;
  const sec = $("v8");
  if (!sec) return;
  const row = sec.querySelector(".page-head + .row") || sec.querySelector(".row");
  if (!row) return;
  const b = document.createElement("button");
  b.className = "btn"; b.id = "rev_dash_btn"; b.textContent = "效果看板";
  b.addEventListener("click", renderReviewDashboard);
  row.appendChild(b);
}

const _origRenderPublishList = renderPublishList;
renderPublishList = function () { _origRenderPublishList(); initReviewDashboardBtn(); };

async function runPublishCheck(id) {
  try {
    const d = V1(await pubApi(`/records/${id}/check`, { method: "POST", body: {} }));
    PUB.rec.check_result = d.items;
    PUB.rec.checked_at = d.checked_at;
    renderPublishDetail();
    toast(d.blocking_passed ? "检查通过，可以进入待发布" : "检查未通过，请看清单补齐");
  } catch (e) { toast("检查失败：" + (e.message || e)); }
}

async function transitionPublish(id, to) {
  const body = { to_status: to };
  if (to === "published") {
    body.published_at = $("pub_at")?.value.trim() || undefined;
    body.publish_url = $("pub_url")?.value.trim() || undefined;
  }
  try {
    PUB.rec = V1(await pubApi(`/records/${id}/transition`, { method: "POST", body }));
    const i = PUB.records.findIndex((r) => r.id === id);
    if (i >= 0) PUB.records[i] = PUB.rec;
    renderPublishList();
    toast("已流转到" + pubStatusLabel(to));
  } catch (e) { toast("流转失败：" + (e.message || e)); }
}

async function setPublishAuth(id) {
  try {
    PUB.rec = V1(await pubApi(`/records/${id}`, { method: "PATCH", body: { authorization_confirmed: true } }));
    renderPublishDetail();
    toast("已确认授权与声明");
  } catch (e) { toast("操作失败：" + (e.message || e)); }
}

async function savePublishNotes(id) {
  try {
    PUB.rec = V1(await pubApi(`/records/${id}`, { method: "PATCH", body: { notes: $("pub_notes").value } }));
    toast("备注已保存");
  } catch (e) { toast("保存失败：" + (e.message || e)); }
}

async function fetchPublishText(id) {
  const token = (typeof AuthClient === "object" && AuthClient.getAccessToken) ? AuthClient.getAccessToken() : "";
  const r = await fetch(`/api/v1/publish/records/${id}/export?org_id=` + encodeURIComponent(S.org.id), {
    headers: token ? { "Authorization": "Bearer " + token } : {},
    credentials: "include",
  });
  if (!r.ok) throw new Error("导出失败（" + r.status + "）");
  return await r.text();
}

async function copyPublishPackage(id) {
  try {
    await navigator.clipboard.writeText(await fetchPublishText(id));
    toast("发布包已复制，去对应平台粘贴发布即可");
  } catch (e) { toast("复制失败：" + (e.message || e)); }
}

async function downloadPublishPackage(id) {
  try {
    const text = await fetchPublishText(id);
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "发布包_" + (PUB.rec?.title || id) + ".txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast("已下载");
  } catch (e) { toast("下载失败：" + (e.message || e)); }
}

document.addEventListener("DOMContentLoaded", init);
/* ================= V3.0 G：跨项目素材库（独立 v9 视图，零 HTML 改动） ================= */
const LIB = { items: [], works: [], destWorkId: "", q: "", mime: "", workFilter: "", busy: false };

function initLibView() {
  if (!document.querySelector('.nav-item[data-v="v9"]')) {
    const navV8 = document.querySelector('.nav-item[data-v="v8"]');
    const b = document.createElement("button");
    b.className = "nav-item"; b.dataset.v = "v9";
    b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 7h3l2-2h6l2 2h3a1 1 0 011 1v10a1 1 0 01-1 1H4a1 1 0 01-1-1V8a1 1 0 011-1z"/></svg>素材库';
    b.addEventListener("click", () => go("v9"));
    if (navV8) navV8.after(b); else document.querySelector(".nav")?.appendChild(b);
  }
  if (!$("v9")) {
    const anchor = $("v8") || $("v7");
    if (anchor) {
      const s = document.createElement("section");
      s.className = "view"; s.id = "v9";
      anchor.after(s);
    }
  }
}

function libApi(path, opts) {
  const sep = path.includes("?") ? "&" : "?";
  const url = "/api/v1/asset-library" + path + sep + "org_id=" + encodeURIComponent(S.org.id);
  const method = (opts && opts.method) || "GET";
  const body = (opts && opts.body) || {};
  if (method === "POST") return ApiClient.post(url, body);
  if (method === "DELETE") return ApiClient.del(url);
  return ApiClient.get(url);
}

function libFmtSize(n) {
  n = n || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function libMimeLabel(m) {
  if ((m || "").startsWith("image/")) return "图片";
  if ((m || "").startsWith("video/")) return "视频";
  if ((m || "").startsWith("audio/")) return "音频";
  return "其他";
}

async function renderAssetLibrary() {
  const sec = $("v9");
  if (!sec) return;
  if (!S.org) { sec.innerHTML = '<div class="page-head"><h2>素材库</h2><p class="muted">请先登录并选择组织</p><div class="page-meander" aria-hidden="true"></div></div>'; return; }
  initLibView();
  sec.innerHTML = viewSkeleton(); // 先给加载态：线上网关延迟下不能留白
  // 作品列表与素材检索互不依赖：并行请求（原为串行，线上高延迟下白屏时间翻倍）
  if (!LIB.destWorkId && S.activeWork && S.activeWork.id) LIB.destWorkId = S.activeWork.id;
  const worksP = ApiClient.get("/api/v1/works?org_id=" + encodeURIComponent(S.org.id))
    .then((r) => { LIB.works = V1(r) || []; })
    .catch(() => { LIB.works = []; });
  await Promise.all([worksP, doLibSearch(true)]);
  renderLibView();
}

async function doLibSearch(silent) {
  if (!S.org) return;
  LIB.busy = true; if (!silent) renderLibView();
  try {
    let url = "/search?";
    const parts = [];
    if (LIB.q) parts.push("q=" + encodeURIComponent(LIB.q));
    if (LIB.mime) parts.push("mime_type=" + encodeURIComponent(LIB.mime));
    if (LIB.workFilter) parts.push("work_id=" + encodeURIComponent(LIB.workFilter));
    url += parts.join("&");
    const r = await libApi(url);
    LIB.items = (V1(r) && V1(r).items) || [];
  } catch (e) { LIB.items = []; }
  LIB.busy = false;
  renderLibView();
  // 图片卡片懒加载缩略图（签名下载 URL）
  document.querySelectorAll("#v9 .lib-thumb[data-aid]").forEach(async (el) => {
    try {
      const d = V1(await ApiClient.get("/api/v1/assets/" + el.dataset.aid + "/download?org_id=" + encodeURIComponent(S.org.id)));
      if (d && d.url) { const img = document.createElement("img"); img.src = d.url; img.alt = ""; el.replaceChildren(img); }
    } catch (e) { /* 无权/无文件时保留类型图标 */ }
  });
}

/* I-10：素材类型图标（1.8px 描边 SVG，替代 emoji） */
const LIB_ICONS = {
  image: '<svg class="lib-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  video: '<svg class="lib-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="7" width="13" height="11" rx="2.5"/><path d="M15.5 10.5l6-3v9l-6-3z"/></svg>',
  audio: '<svg class="lib-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V6l10-2v11"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="15" r="2.5"/></svg>',
  other: '<svg class="lib-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h4"/></svg>',
};
function libIcon(mime) {
  if (mime && mime.startsWith("image/")) return LIB_ICONS.image;
  if (mime && mime.startsWith("video/")) return LIB_ICONS.video;
  if (mime && mime.startsWith("audio/")) return LIB_ICONS.audio;
  return LIB_ICONS.other;
}

/* V3.7：素材按作品归拢——同一作品的素材聚在一组；归属不到作品列表的排最后 */
function libGroupItems() {
  const byId = new Map();
  LIB.works.forEach((w) => byId.set(w.id, { key: w.id, title: w.title || "未命名作品", items: [] }));
  const orphan = [];
  LIB.items.forEach((a) => {
    const g = (a.work_id && byId.get(a.work_id)) || null;
    if (g) g.items.push(a); else orphan.push(a);
  });
  const out = Array.from(byId.values()).filter((g) => g.items.length);
  if (orphan.length) out.push({ key: "_unknown", title: "未归属素材", items: orphan, unknown: true });
  return out;
}

function libCardHtml(a, unknown) {
  return `
    <div class="card lib-card" data-id="${esc(a.id)}">
      ${a.mime && a.mime.startsWith("image/") ? `<div class="lib-thumb" data-aid="${esc(a.id)}">${LIB_ICONS.image}</div>`
        : `<div class="lib-thumb">${libIcon(a.mime)}</div>`}
      <div class="lib-body">
        <strong class="lib-name" title="${esc(a.filename)}">${esc(a.filename)}</strong>
        <div class="muted small">${libMimeLabel(a.mime)} · ${libFmtSize(a.size)} · ${esc((a.created_at || "").slice(0, 16).replace("T", " "))}</div>
        ${unknown ? `<div class="muted small">所属：${esc(a.work_title || "未知项目")}</div>` : ""}
        <div class="small">${a.ref_count > 0 ? `<span class="pill blue">被 ${a.ref_count} 个项目引用</span>` : `<span class="pill gray">未被引用</span>`}</div>
        <div class="lib-actions">
          <button class="btn sm ghost" data-act="detail">详情</button>
          <button class="btn sm primary" data-act="quote">引用到当前项目</button>
        </div>
      </div>
    </div>`;
}

function renderLibView() {
  const sec = $("v9");
  if (!sec) return;
  const workOpts = ['<option value="">全部项目</option>'].concat(
    LIB.works.map((w) => `<option value="${esc(w.id)}"${LIB.workFilter === w.id ? " selected" : ""}>${esc(w.title || "未命名")}</option>`)).join("");
  const destOpts = ['<option value="">— 请选择目标项目 —</option>'].concat(
    LIB.works.map((w) => `<option value="${esc(w.id)}"${LIB.destWorkId === w.id ? " selected" : ""}>${esc(w.title || "未命名")}</option>`)).join("");
  const mimeOpts = ["", "image", "video", "audio", "other"].map((m) =>
    `<option value="${m}"${LIB.mime === m ? " selected" : ""}>${m === "" ? "全部类型" : libMimeLabel(m)}</option>`).join("");
  // V3.7：按作品归拢分组（搜索/筛选结果仍然生效，只改展示方式）
  const groupsHtml = libGroupItems().map((g) => `
    <div class="wgroup">
      <div class="wgroup-head">
        <h3><span class="wg-name">${esc(g.title)}</span></h3>
        <span class="pill ${g.unknown ? "gray" : "blue"}">${g.items.length} 个素材</span>
        ${g.unknown ? "" : `<button class="btn ghost sm wg-filter" data-wg="${esc(g.key)}">${LIB.workFilter === g.key ? "取消只看" : "只看该作品"}</button>`}
        <span class="wg-line"></span>
      </div>
      <div class="lib-grid">${g.items.map((a) => libCardHtml(a, g.unknown)).join("")}</div>
    </div>`).join("");
  sec.innerHTML = `
    <div class="page-head"><h2>素材库</h2>
      <p class="muted">跨项目检索与复用。引用只记关系、不复制文件；删引用 ≠ 删实体——仍被引用的文件不许删除。</p><div class="page-meander" aria-hidden="true"></div></div>
    <div class="toolbar">
      <input id="lib-q" class="input" style="flex:2 1 220px" placeholder="按素材名称搜索…" value="${esc(LIB.q)}" />
      <select id="lib-mime" class="input" style="flex:1 1 130px">${mimeOpts}</select>
      <select id="lib-work" class="input" style="flex:1 1 150px">${workOpts}</select>
      <button class="btn primary" id="lib-search">搜索</button>
      <span class="sep"></span>
      <span class="tl-label">引用到</span>
      <select id="lib-dest" class="input" style="flex:1 1 240px">${destOpts}</select>
      <span class="muted small" style="flex:none">默认为当前打开的项目</span>
    </div>
    ${LIB.busy ? '<div class="card" style="padding:18px;text-align:center"><p class="muted">检索中…</p></div>'
      : (groupsHtml || '<div class="card">' + emptyState("暂无素材", "在「拍摄执行」上传的素材会自动归档到这里。", true, { label: "去上传素材", fn: () => go("v5") }, FRAME_SVG) + '</div>')}`;
  $("lib-q").addEventListener("input", (e) => { LIB.q = e.target.value; });
  $("lib-q").addEventListener("keydown", (e) => { if (e.key === "Enter") doLibSearch(); });
  $("lib-mime").addEventListener("change", (e) => { LIB.mime = e.target.value; doLibSearch(); });
  $("lib-work").addEventListener("change", (e) => { LIB.workFilter = e.target.value; doLibSearch(); });
  $("lib-search").addEventListener("click", () => doLibSearch());
  $("lib-dest").addEventListener("change", (e) => { LIB.destWorkId = e.target.value; });
  sec.querySelectorAll("[data-wg]").forEach((b) => b.addEventListener("click", () => {
    LIB.workFilter = LIB.workFilter === b.dataset.wg ? "" : b.dataset.wg;
    $("lib-work").value = LIB.workFilter;
    doLibSearch();
  }));
  sec.querySelectorAll(".lib-card").forEach((el) => {
    el.querySelector('[data-act="detail"]').addEventListener("click", () => showLibDetail(el.dataset.id));
    el.querySelector('[data-act="quote"]').addEventListener("click", () => libQuote(el.dataset.id));
  });
}

async function libQuote(assetId) {
  if (!LIB.destWorkId) { toast("请先选择要引用到的目标项目"); return; }
  try {
    await libApi("/refs", { method: "POST", body: { org_id: S.org.id, asset_id: assetId, dest_work_id: LIB.destWorkId } });
    toast("已引用到目标项目");
    await doLibSearch();
  } catch (e) { toast("引用失败：" + (e.message || e)); }
}

async function showLibDetail(assetId) {
  let d;
  try { d = V1(await libApi("/assets/" + encodeURIComponent(assetId))); }
  catch (e) { toast("加载失败：" + (e.message || e)); return; }
  const rows = (d.refs || []).map((r) => `
    <div class="lib-ref-row">
      <span>引用到「${esc(r.dest_work_title || r.dest_work_id)}」</span>
      <span class="muted small">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</span>
      <button class="btn sm danger" data-ref="${esc(r.id)}">解除引用</button>
    </div>`).join("");
  let m = $("lib_detail_modal");
  if (!m) {
    m = document.createElement("div");
    m.className = "modal-mask"; m.id = "lib_detail_modal";
    m.innerHTML = `<div class="modal" style="max-width:640px"><h3>素材详情</h3>
      <div id="lib_detail_body" style="max-height:60vh;overflow:auto"></div>
      <div style="display:flex;margin-top:16px"><button class="btn ghost sm" id="lib_detail_close" style="margin-left:auto">关闭</button></div></div>`;
    document.body.appendChild(m);
    m.addEventListener("click", (e) => { if (e.target === m || e.target.id === "lib_detail_close") m.classList.remove("show"); });
  }
  $("lib_detail_body").innerHTML = `
    <p><strong>${esc(d.filename)}</strong></p>
    <p class="muted small">${libMimeLabel(d.mime)} · ${libFmtSize(d.size)} · 上传 ${esc((d.created_at || "").slice(0, 16).replace("T", " "))}</p>
    <p>来源项目：<strong>${esc(d.source_work_title || d.source_work_id)}</strong></p>
    <h4>引用关系（${d.ref_count} 个项目在用）</h4>
    <div class="lib-refs">${rows || '<p class="muted">暂无其他项目引用</p>'}</div>
    <p class="muted small">提示：仍被引用的文件不能直接删除；解除全部引用后才能删除实体。</p>`;
  m.classList.add("show");
  m.querySelectorAll(".lib-ref-row [data-ref]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!(await modalConfirm({ title: "解除引用", body: "解除该引用？只删引用关系，不删文件实体。", okText: "解除", danger: true }))) return;
      try {
        await libApi("/refs/" + encodeURIComponent(b.dataset.ref), { method: "DELETE" });
        toast("引用已解除");
        m.classList.remove("show");
        showLibDetail(assetId); doLibSearch();
      } catch (e) { toast("解除失败：" + (e.message || e)); }
    });
  });
}

})();
