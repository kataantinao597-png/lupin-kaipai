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

/* 用户自带 Key（只存 sessionStorage，关闭标签页清除）——ApiClient 会自动读取 */
function getApiConfig() {
  try {
    return {
      base: sessionStorage.getItem("lupin_api_base") || "",
      key: sessionStorage.getItem("lupin_api_key") || "",
      model: sessionStorage.getItem("lupin_api_model") || "",
    };
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
  workFilter: "all",
  authMode: "login",
  ideaSel: -1,
};

function blankData() {
  return { product: null, storyboard: null, shoot_state: {}, feasibility: null, risk: null,
           ideas: [], direction: "", poster: null, ideasFactsHash: "", sbFactsHash: "", shoot_note: "" };
}

/* ---------- 路由（含登录守卫） ---------- */
const NEED_AUTH = ["v1", "v2", "v3", "v4", "v5", "v6"];
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
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (v !== "v0" && v !== "v-login") revealIn(sec);
  onEnter(v);
}
window.go = go;

function onEnter(v) {
  if (v === "v1") renderWorkbench();
  else if (v === "v2") { fillProductForm(); loadFacts(); }
  else if (v === "v3") { renderStepper(); renderIdeas(); renderShots(); }
  else if (v === "v4") { renderPoster(); }
  else if (v === "v5") { renderShoot(); loadAssets(); fillNote(); }
  else if (v === "v6") { renderDeliveryTitles(); loadTimeline(); }
}

/* ---------- 认证 ---------- */
function setAuthMode(mode) {
  S.authMode = mode;
  $("tab_login").classList.toggle("active", mode === "login");
  $("tab_register").classList.toggle("active", mode === "register");
  $("row_display_name").style.display = mode === "register" ? "" : "none";
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
  if (orgs.length) { S.org = orgs[0]; return; }
  const name = (S.user && (S.user.display_name || S.user.email.split("@")[0]) || "我") + "的工作室";
  S.org = V1(await ApiClient.post("/api/v1/orgs", { name }));
}

async function doLogout() {
  await AuthClient.logout();
  S.user = null; S.org = null; S.works = []; S.activeWork = null; S.data = blankData(); S.facts = [];
  renderUser();
  go("v-login");
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
}

async function createWork(name) {
  const title = (name || "").trim() || ("作品 " + (S.works.length + 1));
  const w = V1(await ApiClient.post("/api/v1/works", { org_id: S.org.id, title, data: blankData() }));
  S.activeWork = w;
  S.data = blankData();
  await loadWorks();
  S.activeWork = S.works.find((x) => x.id === w.id) || w;
  S.data = blankData();
  Object.assign(S.data, JSON.parse(JSON.stringify(S.activeWork.data || {})));
  return S.activeWork;
}

async function saveWork() {
  if (!S.activeWork) return;
  try {
    const w = V1(await ApiClient.patch("/api/v1/works/" + S.activeWork.id,
      { data: S.data, version: S.activeWork.version }));
    S.activeWork = w;
    const i = S.works.findIndex((x) => x.id === w.id);
    if (i >= 0) S.works[i] = w;
  } catch (e) {
    if (e && e.status === 409) {
      toast("作品在别处被修改，已重新加载最新版本");
      await loadWorks();
      renderWorkbench();
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
  if (!confirm(`确定删除作品「${w.title}」吗？`)) return;
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
  return { pct, stage, shots: shots.length, done };
}

function nextViewFor(d) {
  if (!d.product) return "v2";
  if (!(d.ideas || []).length) return "v3";
  if (!((d.storyboard || {}).shots || []).length) return "v3";
  return "v5";
}

/* ---------- v1 工作台 ---------- */
const COVERS = [
  "linear-gradient(135deg,#C0392B,#E67E22)", "linear-gradient(135deg,#1FA97A,#A3D65C)",
  "linear-gradient(135deg,#2E6BF0,#7C5CFC)", "linear-gradient(135deg,#7C4A1E,#D9A441)",
];
async function renderWorkbench() {
  if (!S.org) return;
  try {
    await loadWorks();
  } catch (e) {
    $("greet_text").textContent = "加载失败：" + (e.message || "网络错误");
    return;
  }
  // 问候语：真实数字
  const activeCount = S.works.filter((w) => (w.status || "draft") !== "archived").length;
  let aiCalls = null;
  try {
    const sum = RAW(await ApiClient.get("/api/v1/ai/usage/summary?days=1"));
    aiCalls = sum && sum.today ? sum.today.calls : null;
  } catch (e) { /* 用量接口失败不阻塞 */ }
  const hour = new Date().getHours();
  const hi = hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
  $("greet_title").textContent = `${hi}，让山东好产品，被更多人看见！`;
  $("greet_text").innerHTML =
    `你有 <b>${activeCount}</b> 个作品正在推进中` +
    (aiCalls === null ? `。` : `，今日已生成 <b>${aiCalls}</b> 次 AI 方案。`);

  // 作品卡片
  const grid = $("work_grid");
  let items = S.works.slice();
  if (S.workFilter === "active") items = items.filter((w) => (w.status || "draft") !== "archived");
  if (S.workFilter === "done") items = items.filter((w) => (w.status || "") === "archived");
  if (!items.length) {
    grid.innerHTML = '<p style="color:var(--muted);grid-column:1/-1">暂无作品，点击「创建新作品」开始。</p>';
  } else {
    grid.innerHTML = items.map((w, i) => {
      const d = w.data || {};
      const p = workProgress(d);
      const name = (d.product && d.product.product_name) || w.title;
      const done = w.status === "archived";
      return `<div class="card work" data-id="${esc(w.id)}">
        <div class="cover" style="background:${COVERS[i % COVERS.length]}"><span class="pill ${done ? "green" : "amber"}">${done ? "已归档" : "进行中"}</span></div>
        <div class="body"><h4>${esc(name)}</h4><div class="date">${esc((w.updated_at || "").slice(0, 16).replace("T", " "))}</div>
        <div class="meta"><span>${esc(p.stage)}</span><span>${p.pct}%</span></div><div class="progress"><i style="width:${p.pct}%"></i></div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn ghost sm" data-act="open" style="flex:1;justify-content:center">继续创作</button>
          <button class="btn ghost sm" data-act="del" title="删除">删</button>
        </div></div></div>`;
    }).join("");
  }
  grid.querySelectorAll(".work").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector('[data-act="open"]').addEventListener("click", (e) => { e.stopPropagation(); openWork(id); });
    card.querySelector('[data-act="del"]').addEventListener("click", (e) => { e.stopPropagation(); deleteWork(id); });
  });
}

async function newWorkFlow() {
  if (!loggedIn()) { go("v-login"); return; }
  const name = prompt("请输入作品名称：", "作品 " + (S.works.length + 1));
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
  return S.facts.filter(factUsable).map((f) => ({ content: f.content, category: f.category, source_ref: f.source_ref }));
}
function factsHash() {
  return usableFacts().map((f) => f.content).join("|");
}

async function loadFacts() {
  if (!S.org) return;
  try {
    S.facts = V1(await ApiClient.get("/api/v1/facts?org_id=" + encodeURIComponent(S.org.id))) || [];
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
    list.innerHTML = '<p style="font-size:12.5px;color:var(--muted)">暂无事实。添加已确认的产品信息，AI 生成时将优先使用有依据的内容。</p>';
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
      <span class="pill ${tone}">${label}</span></div>
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
      const reason = prompt("确认理由（必填）：", "已核实");
      if (reason === null) return;
      if (!reason.trim()) { toast("确认需要填写理由"); return; }
      // 后端规则：确认事实必须设置 allow_use=true（作为宣传依据）
      if (!confirm("确认后，这条事实将被标记为「允许用于 AI 生成」。继续吗？")) return;
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "confirmed", reason: reason.trim(), allow_use: true });
    } else if (act === "reject") {
      const reason = prompt("驳回理由（必填）：");
      if (reason === null) return;
      if (!reason.trim()) { toast("驳回需要填写理由"); return; }
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "rejected", reason: reason.trim() });
    } else if (act === "revoke") {
      const reason = prompt("撤销理由（必填）：");
      if (reason === null) return;
      if (!reason.trim()) { toast("撤销需要填写理由"); return; }
      await ApiClient.post(`/api/v1/facts/${id}/transition`, { org_id: S.org.id, target_status: "revoked", reason: reason.trim() });
    } else if (act === "edit") {
      const content = prompt("编辑事实内容（将生成新版本，旧版本保留）：", f.content);
      if (content === null || !content.trim() || content.trim() === f.content) return;
      await ApiClient.patch(`/api/v1/facts/${id}/content`, { org_id: S.org.id, expected_version: f.version, content: content.trim() });
      toast("已保存为新版本，旧版本可在变更记录查看");
    } else if (act === "history") {
      const h = V1(await ApiClient.get(`/api/v1/facts/${id}/history?org_id=` + encodeURIComponent(S.org.id))) || [];
      const txt = h.length ? h.map((x) => `${(x.created_at || "").slice(0, 16).replace("T", " ")}　${x.from_status || ""}→${x.to_status || ""}　${x.reason || ""}`).join("\n") : "暂无变更记录";
      alert(`「${f.content.slice(0, 30)}…」的变更记录：\n\n${txt}`);
      return;
    }
    await loadFacts();
  } catch (e) {
    toast("操作失败：" + (e.message || "网络错误"));
  }
}

async function saveFact() {
  const content = $("f_content").value.trim();
  if (!content) { errText("fact_err", "请填写事实内容"); return; }
  try {
    await ApiClient.post("/api/v1/facts", {
      org_id: S.org.id, content, category: $("f_category").value,
      source_type: "human_statement", source_ref: $("f_source").value.trim(),
    });
    // 新建默认为草稿；若勾选允许用于生成，提交审核时再确认
    $("fact_editor").style.display = "none";
    $("f_content").value = ""; $("f_source").value = "";
    await loadFacts();
    toast("已添加（草稿），提交审核并确认后可用于 AI 生成");
  } catch (e) {
    errText("fact_err", e.message || "保存失败");
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
  if (w3) w3.innerHTML = staleIdeas ? `<div class="notice" style="margin:0 0 14px">产品事实已更新，这些创意可能基于旧资料，建议重新生成。</div>` : "";
  const wsb = $("sb_stale_warn");
  if (wsb) wsb.innerHTML = staleSb ? `<div class="notice" style="margin:0 0 14px">产品事实已更新，此分镜可能基于旧资料，建议重新生成。</div>` : "";
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
  aiLoading("正在生成 3 个创意方向…");
  try {
    const d = RAW(await ApiClient.post("/api/ideas", { product: p, facts: usableFacts() }));
    S.data.ideas = d.ideas || [];
    S.data.ideasFactsHash = factsHash();
    S.ideaSel = -1;
    await saveWork();
    renderIdeas();
    checkFactsStale();
    toast(`已生成 ${S.data.ideas.length} 个创意方向`);
  } catch (e) {
    errText("ideas_err", e.message || "生成失败");
  } finally {
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
    cards.innerHTML = '<p style="font-size:13px;color:var(--muted);grid-column:1/-1">先在「产品资料」页填写产品信息并保存，然后点击「生成创意方案」。AI 生成约需半分钟。</p>';
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
    <div class="idea-card ${i === S.ideaSel ? "sel" : ""}" data-i="${i}">
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
  cards.querySelectorAll(".idea-card").forEach((c) => c.addEventListener("click", async () => {
    S.ideaSel = +c.dataset.i;
    const it = ideas[S.ideaSel];
    S.data.direction = (it.name || "") + "：" + (it.tagline || "");
    await saveWork();
    renderIdeas();
    toast("已选择该方向，可点击「生成分镜」");
  }));
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
  errText("sb_err", "");
  aiLoading("正在生成结构化分镜…（约半分钟）");
  try {
    const d = RAW(await ApiClient.post("/api/storyboard", { product: p, direction, facts: usableFacts() }));
    S.data.storyboard = d.storyboard;
    S.data.direction = direction;
    S.data.sbFactsHash = factsHash();
    await saveWork();
    renderShots();
    checkFactsStale();
    toast(`已生成 ${(d.storyboard.shots || []).length} 个镜头`);
  } catch (e) {
    errText("sb_err", e.message || "生成失败");
  } finally {
    aiLoadingDone();
  }
}

function renderShots() {
  const sb = S.data.storyboard;
  const tb = $("sb_tbody");
  if (!sb || !(sb.shots || []).length) {
    tb.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">暂无分镜，请先选择创意方向并生成。</td></tr>';
    $("sb_meta").textContent = "";
    return;
  }
  const shots = sb.shots;
  const total = shots.reduce((a, s) => a + (parseFloat(s.duration_seconds) || 0), 0);
  $("sb_meta").textContent = `共 ${shots.length} 个镜头 · 总时长 ${Math.round(total)} 秒`;
  tb.innerHTML = shots.map((s, i) => `
    <tr>
      <td>${s.shot_number || i + 1}</td>
      <td>${esc(s.shot_type || "—")}</td>
      <td>${esc(s.scene_description || "—")}</td>
      <td>${esc(s.shooting_action || "—")}</td>
      <td>${esc(String(s.duration_seconds ?? "—"))}s</td>
      <td>${esc(s.voiceover || s.subtitle || "—")}</td>
      <td><div class="op">
        <button data-shot="edit" data-i="${i}">编辑</button>
        <button data-shot="regen" data-i="${i}">重拍</button>
        <button data-shot="del" data-i="${i}">删除</button>
      </div></td>
    </tr>`).join("");
  tb.querySelectorAll("[data-shot]").forEach((b) => b.addEventListener("click", () => shotAction(b.dataset.shot, +b.dataset.i)));
}

async function shotAction(act, i) {
  const shots = S.data.storyboard.shots;
  const s = shots[i];
  if (act === "del") {
    if (!confirm(`删除镜头 ${s.shot_number}？`)) return;
    shots.splice(i, 1);
    shots.forEach((x, j) => { x.shot_number = j + 1; });
    await saveWork(); renderShots();
  } else if (act === "edit") {
    const scene = prompt("画面描述：", s.scene_description || "");
    if (scene === null) return;
    const vo = prompt("台词/口播：", s.voiceover || "");
    if (vo === null) return;
    const dur = prompt("时长（秒）：", String(s.duration_seconds || ""));
    if (dur === null) return;
    s.scene_description = scene.trim();
    s.voiceover = vo.trim();
    const d = parseFloat(dur);
    if (d > 0) s.duration_seconds = d;
    await saveWork(); renderShots();
    toast("已更新");
  } else if (act === "regen") {
    const instruction = prompt(`对镜头 ${s.shot_number} 的修改要求（例如：改成俯视角度）：`);
    if (!instruction || !instruction.trim()) return;
    aiLoading(`正在重生成镜头 ${s.shot_number}…`);
    try {
      const d = RAW(await ApiClient.post("/api/regenerate-shot", {
        storyboard: S.data.storyboard, shot_number: s.shot_number,
        instruction: instruction.trim(), locked: [],
      }));
      S.data.storyboard = d.storyboard;
      await saveWork(); renderShots();
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
  const scene = prompt("新镜头的画面描述：");
  if (!scene || !scene.trim()) return;
  const dur = parseFloat(prompt("时长（秒）：", "5")) || 5;
  sb.shots.push({ shot_number: sb.shots.length + 1, shot_type: "中景", scene_description: scene.trim(),
    shooting_action: "", camera_movement: "", voiceover: "", subtitle: "", duration_seconds: dur, props: [], required_assets: [] });
  await saveWork(); renderShots();
}

/* ---------- 风险 / 可行性 ---------- */
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
    await saveWork();
    renderRisks();
    if (!S.data.risk.length) $("risk_results").innerHTML = '<p style="font-size:13px;color:var(--muted)">未发现明显风险提示（仍建议人工通读旁白/字幕复核）。</p>';
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
  $("risk_results").innerHTML = risks.map((f, i) => `
    <div class="risk-item">
      <input type="checkbox" data-ri="${i}" ${f.confirmed ? "checked" : ""} title="人工确认">
      <div><div><span class="pill ${lv[f.level] || "gray"}">${esc(f.level || "")}风险</span>
      <span class="pill gray">${esc(f.source || "")}</span> ${esc(f.text || "")}</div>
      <div style="font-size:12.5px;color:var(--muted);margin-top:4px">${esc(f.reason || "")}</div></div>
    </div>`).join("");
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
    `<p style="font-size:13px;color:var(--muted);margin-bottom:12px">共 ${results.length} 镜头：` +
    ["可执行", "建议调整", "不可执行", "需要人工确认"].map((s) => `${s} ${counts[s] || 0}`).join(" · ") + `</p>` +
    `<p style="font-size:12.5px;color:var(--muted)">本检查为拍摄可行性提示，不构成绝对保证；最终以实际拍摄条件为准。</p>` +
    results.map((r) => `
    <div class="risk-item ${tone[r.status] || ""}">
      <div style="flex:1"><div><b>镜头 ${r.shot_number}</b> <span class="pill ${r.status === "可执行" ? "green" : r.status === "不可执行" ? "red" : "amber"}">${esc(r.status)}</span>
      <span class="pill gray">${esc(r.source || "")}判断</span></div>
      <div style="font-size:12.5px;color:var(--ink2);margin-top:4px">${esc(r.reason || "")}</div>
      ${(r.constraints_involved || []).length ? `<div style="font-size:12px;color:var(--muted)">涉及约束：${esc(r.constraints_involved.join("、"))}</div>` : ""}
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
};
}
function renderPoster() {
const st = posterState();
$("pe_title").value = st.title;
$("pe_sub").value = st.sub;
$("pe_promo").value = st.promo;
$("pe_btn").value = st.btn;
$("pe_copy").value = st.copy;
document.querySelectorAll("#poster_tpls.t").forEach((t) => t.classList.toggle("active", t.dataset.tpl === st.tpl));
updatePosterPreview();
}
function updatePosterPreview() {
const tpl = document.querySelector("#poster_tpls.t.active");
const key = tpl? tpl.dataset.tpl: "harvest";
const bg = (POSTER_TPLS[key] || POSTER_TPLS.harvest).bg;
$("poster_preview").style.background = bg;
const title = $("pe_title").value.trim() || "产品名称";
const sub = $("pe_sub").value.trim();
$("pp_title").innerHTML = esc(title).replace(/\n/g, "<br>") + (sub? "<br>" + esc(sub): "");
$("pp_promo").textContent = $("pe_promo").value.trim() || "促 销 信 息";
$("pp_btn").textContent = ($("pe_btn").value.trim() || "行动按钮") + " →";
}
function collectPoster() {
const tpl = document.querySelector("#poster_tpls.t.active");
return { tpl: tpl? tpl.dataset.tpl: "harvest", title: $("pe_title").value.trim(),
sub: $("pe_sub").value.trim(), promo: $("pe_promo").value.trim(),
btn: $("pe_btn").value.trim(), copy: $("pe_copy").value.trim()};
}
async function savePosterDraft() {
if (!S.activeWork) { toast("请先创建作品"); return;}
S.data.poster = collectPoster();
await saveWork();
$("poster_msg").textContent = "✓ 草稿已保存到当前作品";
setTimeout(() => { $("poster_msg").textContent = "";}, 2500);
}
function exportPosterPNG() {
const st = collectPoster();
const W = 1080, H = 1920;
const cv = document.createElement("canvas");
cv.width = W; cv.height = H;
const ctx = cv.getContext("2d");
// 背景：按模板取首尾色做竖向渐变
const colorStops = { harvest: ["#6E2A18", "#C07F33"], ocean: ["#12395E", "#2E7BB0"],
orchard: ["#1E3A24", "#5E9E52"], grape: ["#3A1E5E", "#9E6BD8"]};
const [c1, c2] = colorStops[st.tpl] || colorStops.harvest;
const g = ctx.createLinearGradient(0, 0, 0, H);
g.addColorStop(0, c1); g.addColorStop(1, c2);
ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
// 年画红框（与页面预览一致）
ctx.strokeStyle = "#B23727"; ctx.lineWidth = 28; ctx.strokeRect(14, 14, W - 28, H - 28);
ctx.fillStyle = "#fff"; ctx.textAlign = "center";
const title = (st.title || "产品名称") + (st.sub? "\n" + st.sub: "");
ctx.font = "900 110px 'Noto Sans SC', sans-serif";
const lines = title.split("\n");
lines.forEach((ln, i) => ctx.fillText(ln, W / 2, H - 620 + i * 140));
ctx.font = "500 44px 'Noto Sans SC', sans-serif";
ctx.fillStyle = "#FFF6E3";
ctx.fillText((st.promo || "促销信息").split("").join(" "), W / 2, H - 380);
// 按钮
ctx.fillStyle = "#fff";
const bw = 420, bh = 110, bx = (W - bw) / 2, by = H - 300;
ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 55); ctx.fill();
ctx.fillStyle = "#B4502E"; ctx.font = "700 48px 'Noto Sans SC', sans-serif";
ctx.fillText((st.btn || "立即抢购") + " →", W / 2, by + 74);
const a = document.createElement("a");
a.href = cv.toDataURL("image/png");
a.download = `海报_${st.title || "未命名"}.png`;
document.body.appendChild(a); a.click(); a.remove();
toast("海报 PNG 已导出");
}

/* ================= v5 拍摄执行 ================= */
function shootKey(s, i) { return "s" + (s.shot_number || i + 1);}
function renderShoot() {
const sb = S.data.storyboard;
const list = $("shoot_list");
if (!sb ||!(sb.shots || []).length) {
list.innerHTML = '<p style="font-size:13px;color:var(--muted)">暂无分镜，请先在「创意策划」页生成分镜。</p>';
$("shoot_progress").textContent = "拍摄进度";
$("shoot_bar").style.width = "0%"; $("shoot_pct").textContent = "0%";
return;
}
const shots = sb.shots;
const st = S.data.shoot_state || {};
let done = 0;
list.innerHTML = shots.map((s, i) => {
const k = shootKey(s, i);
const cur = (st[k] || {}).status || "todo";
if (cur === "done") done++;
const pill = cur === "done"? '<span class="note pill green">已完成</span>': '<span class="note pill amber">待拍摄</span>';
return `<div class="check ${cur === "done"? "done": ""}" data-k="${k}">
<span class="box"></span>
<span class="t"><b>镜头 ${s.shot_number || i + 1} · ${esc(s.scene_description || "")}</b><br>
<span style="color:var(--muted);font-size:12.5px">${esc(String(s.duration_seconds || ""))}s · ${esc(s.shooting_action || "")} · ${esc(s.camera_movement || "")}</span></span>
${pill}</div>`;
}).join("");
list.querySelectorAll(".check").forEach((c) => c.addEventListener("click", async () => {
const k = c.dataset.k;
const cur = ((S.data.shoot_state || {})[k] || {}).status || "todo";
S.data.shoot_state[k] = { status: cur === "done"? "todo": "done", note: ""};
await saveWork();
renderShoot();
}));
const pct = shots.length? Math.round((done / shots.length) * 100): 0;
$("shoot_progress").textContent = `拍摄进度 ${done}/${shots.length}`;
$("shoot_bar").style.width = pct + "%";
$("shoot_pct").textContent = pct + "%";
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
grid.innerHTML = '<p style="font-size:12.5px;color:var(--muted);grid-column:1/-1">暂无素材，点击下方按钮上传。</p>';
return;
}
grid.innerHTML = list.map((a) => {
const isImg = (a.mime || "").startsWith("image/");
return `<div class="mat" data-aid="${esc(a.id)}" data-img="${isImg? 1: 0}" title="${esc(a.filename)}"
style="background:linear-gradient(135deg,#5B6B7A,#A8B8C8);display:grid;place-items:center;color:#fff;font-size:11px;overflow:hidden">
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
<div class="check" data-di="${i}"><span class="box"></span>
<span class="t"><b>${esc(it.label)}</b><br><span style="color:var(--muted);font-size:12.5px">${esc(it.reason || "")}</span></span>
<span class="note pill ${pillTone[it.status] || "gray"}">${esc(labels[it.status] || it.status)}</span></div>`).join("") +
`<p style="font-size:12px;color:var(--muted);margin-top:10px">勾选 = 人工已确认。风险提示仅为依据缺失提醒，本工具不保证广告合规，发布前请自行把关。</p>`;
$("delivery_results").querySelectorAll(".check").forEach((c) => c.addEventListener("click", () => c.classList.toggle("done")));
} catch (e) {
errText("delivery_err", e.message || "检查失败");
} finally {
aiLoadingDone();
}
}

async function loadTimeline() {
const panel = $("timeline_panel");
if (!S.activeWork) { panel.innerHTML = '<p style="font-size:13px;color:var(--muted)">请先创建作品。</p>'; return;}
try {
const t = V1(await ApiClient.get(`/api/v1/works/${S.activeWork.id}/timeline`));
if (!t ||!(t.shots || []).length) {
panel.innerHTML = '<p style="font-size:13px;color:var(--muted)">暂无分镜时间轴，请先生成分镜。</p>';
return;
}
panel.innerHTML =
`<p style="font-size:13px;margin-bottom:8px">目标 ${t.target_duration_seconds}s · 实际 ${Math.round(t.total_duration_seconds)}s · ` +
`<span class="pill ${t.within_tolerance? "green": "amber"}">${t.within_tolerance? "时长达标": (t.diff_seconds > 0? `超时 ${Math.round(t.diff_seconds)}s`: `不足 ${Math.round(-t.diff_seconds)}s`)}</span></p>` +
(() => { let c = 0;
return t.shots.map((s) => {
const d = parseFloat(s.duration_seconds) || 0;
const st = c; c += d;
return `<div class="tl-item"><span class="tl-time">${Math.round(st)}s – ${Math.round(c)}s</span><span>镜头 ${s.shot_no} · ${Math.round(d)}s</span></div>`;
}).join(""); })();
} catch (e) {
panel.innerHTML = `<p style="font-size:13px;color:var(--muted)">时间轴加载失败：${esc(e.message || "")}</p>`;
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
function openCfg() {
const cfg = getApiConfig();
$("cfg_base").value = cfg.base;
$("cfg_key").value = "";
$("cfg_model").value = cfg.model;
updateCfgStatus();
$("cfg_modal").classList.add("show");
}
function updateCfgStatus() {
const cfg = getApiConfig();
$("cfg_status").textContent = cfg.key? "✓ 已配置自定义 Key（仅当前标签页有效，关闭即清除）": "当前使用服务端默认配置";
}
function saveCfg() {
try {
const base = $("cfg_base").value.trim(), key = $("cfg_key").value.trim(), model = $("cfg_model").value.trim();
if (base &&!/^https?:\/\//i.test(base)) { toast("Base URL 必须以 http(s):// 开头"); return;}
if (base) sessionStorage.setItem("lupin_api_base", base); else sessionStorage.removeItem("lupin_api_base");
if (key) sessionStorage.setItem("lupin_api_key", key); else sessionStorage.removeItem("lupin_api_key");
if (model) sessionStorage.setItem("lupin_api_model", model); else sessionStorage.removeItem("lupin_api_model");
$("cfg_key").value = "";
updateCfgStatus();
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

/* ================= 事件绑定 ================= */
function bindAll() {
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
$("qa_new").addEventListener("click", newWork);
$("btn_new_work").addEventListener("click", newWork);
$("btn_new_work_top").addEventListener("click", newWork);
document.querySelectorAll("#v1 [data-filter]").forEach((p) => p.addEventListener("click", () => {
S.workFilter = p.dataset.filter;
document.querySelectorAll("#v1 [data-filter]").forEach((x) => {
const on = x === p;
x.classList.toggle("blue", on); x.classList.toggle("gray",!on);
});
renderWorkbench();
}));

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
$("btn_run_feas").addEventListener("click", runFeas);

document.querySelectorAll("#poster_tpls.t").forEach((t) => t.addEventListener("click", () => {
document.querySelectorAll("#poster_tpls.t").forEach((x) => x.classList.remove("active"));
t.classList.add("active");
updatePosterPreview();
}));
["pe_title", "pe_sub", "pe_promo", "pe_btn"].forEach((id) => $(id).addEventListener("input", updatePosterPreview));
$("btn_gen_poster").addEventListener("click", () => toast("图片生成能力待接入：当前版本支持模板排版与 PNG 导出，AI 生图后续版本支持"));
$("btn_poster_save").addEventListener("click", savePosterDraft);
$("btn_poster_png").addEventListener("click", exportPosterPNG);
$("btn_copy_copy").addEventListener("click", async () => {
const txt = $("pe_copy").value;
if (!txt.trim()) { toast("文案为空"); return;}
try { await navigator.clipboard.writeText(txt); toast("已复制到剪贴板");}
catch (e) { toast("复制失败，请手动选择复制");}
});

$("btn_print").addEventListener("click", () => window.print());
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

$("btn_delivery_check").addEventListener("click", runDeliveryCheck);
$("exp_docx").addEventListener("click", exportDocx);
$("exp_srt").addEventListener("click", exportSrt);
$("exp_txt").addEventListener("click", exportTxt);
$("btn_export_all").addEventListener("click", async () => {
if (!needStoryboard()) return;
await exportDocx(); await exportSrt(); exportTxt();
});

$("nav_api_cfg").addEventListener("click", openCfg);
$("btn_save_cfg").addEventListener("click", saveCfg);
$("btn_clear_cfg").addEventListener("click", () => {
try { sessionStorage.removeItem("lupin_api_base"); sessionStorage.removeItem("lupin_api_key"); sessionStorage.removeItem("lupin_api_model");} catch (e) {}
$("cfg_base").value = ""; $("cfg_key").value = ""; $("cfg_model").value = "";
updateCfgStatus(); toast("已清除");
});
$("btn_close_cfg").addEventListener("click", () => $("cfg_modal").classList.remove("show"));
$("cfg_modal").addEventListener("click", (e) => { if (e.target === $("cfg_modal")) $("cfg_modal").classList.remove("show");});
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
go("v0");
}
revealIn($("v0"));
}

document.addEventListener("DOMContentLoaded", init);
})();
