/* 鲁品开拍 - 前端逻辑（原生 JS） */
const state = { product: null, ideas: [], direction: "", storyboard: null, risks: [], locked: new Set() };
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
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("请求失败 HTTP " + r.status));
  return data;
}
const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/* ---------- 视图1：表单 ---------- */
function collectForm() {
  const lines = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
  const name = $("f_name").value.trim();
  if (!name) throw new Error("请填写产品名称");
  return {
    product_name: name,
    description: $("f_desc").value.trim(),
    selling_points: lines("f_points"),
    price: $("f_price").value.trim(),
    audience: $("f_audience").value.trim(),
    evidence_materials: $("f_evidence").value.trim(),
    shoot: {
      device: $("f_device").value,
      people: parseInt($("f_people").value) || 1,
      lighting: $("f_lighting").value.trim(),
      location: $("f_location").value.trim(),
      target_duration_seconds: parseFloat($("f_duration").value) || 60,
      note: $("f_note").value.trim(),
    },
  };
}
$("btn_fill_demo").addEventListener("click", () => {
  $("f_name").value = "烟台红富士苹果";
  $("f_desc").value = "山东烟台产红富士，套袋种植";
  $("f_points").value = "脆甜多汁\n果园现摘现发\n套袋种植，农药残留顾虑小";
  $("f_price").value = "10斤装 39.9元";
  $("f_audience").value = "25-40岁宝妈，注重孩子饮食健康";
  $("f_device").value = "手机"; $("f_people").value = 2;
  $("f_location").value = "果园、家中厨房"; $("f_lighting").value = "自然光";
  $("f_duration").value = 60; $("f_note").value = "";
});
$("btn_ideas").addEventListener("click", async () => {
  showErr("err1", "");
  try {
    state.product = collectForm();
    state.locked = new Set();
    setLoading("正在生成 3 个创意方向…");
    const d = await api("/api/ideas", { product: state.product });
    state.ideas = d.ideas;
    renderIdeas();
    showView("v2");
  } catch (e) { showErr("err1", e.message); }
  finally { setLoading(null); }
});

/* ---------- 视图2：创意方向 ---------- */
function renderIdeas() {
  $("ideas").innerHTML = state.ideas.map((it, i) => `
    <div class="card" id="idea_${i}">
      <h4>${esc(it.name)}</h4>
      <div class="row"><span class="k">创意点</span><span>${esc(it.tagline)}</span></div>
      <div class="row"><span class="k">开场3秒</span><span>${esc(it.hook_3s)}</span></div>
      <div class="row"><span class="k">核心思路</span><span>${esc(it.core_idea)}</span></div>
      <div class="row"><span class="k">受众契合</span><span>${esc(it.audience_fit)}</span></div>
      <div class="row"><span class="k">拍摄难度</span><span>${esc(it.difficulty)}</span></div>
      <div class="row"><span class="k">素材需求</span><span>${esc(it.materials_needed)}</span></div>
      <div class="actions"><button class="primary" onclick="selectIdea(${i})">选择这个方向并生成分镜 →</button></div>
    </div>`).join("");
}
async function selectIdea(i) {
  showErr("err2", "");
  const it = state.ideas[i];
  state.direction = it.name + "：" + it.tagline;
  try {
    setLoading("正在生成结构化分镜…（约半分钟）");
    const d = await api("/api/storyboard", { product: state.product, direction: state.direction });
    state.storyboard = d.storyboard;
    state.risks = [];
    renderShots();
    await refreshValidate();
    showView("v3");
  } catch (e) { showErr("err2", e.message); }
  finally { setLoading(null); }
}
$("btn_back1").addEventListener("click", () => showView("v1"));
$("btn_regen_ideas").addEventListener("click", () => { showView("v1"); $("btn_ideas").click(); });

/* ---------- 视图3：分镜编辑器 ---------- */
function renumber() {
  state.storyboard.shots.forEach((s, i) => { s.shot_number = i + 1; });
}
async function refreshValidate() {
  const bar = $("validate_bar");
  try {
    const d = await api("/api/validate", { storyboard: state.storyboard });
    const total = d.total_seconds;
    const target = state.storyboard.target_duration_seconds;
    if (d.valid) {
      bar.className = "validate-bar ok";
      bar.textContent = `✓ 校验通过：${d.shot_count} 个镜头，总时长 ${total}s（目标 ${target}s）`;
    } else {
      bar.className = "validate-bar bad";
      bar.textContent = "✗ 校验未通过：" + d.errors.slice(0, 3).join("；");
    }
    const sbm = $("sb_meta");
    sbm.textContent = `（${d.shot_count} 镜头 / ${total}s）`;
  } catch (e) { bar.className = "validate-bar bad"; bar.textContent = "校验请求失败：" + e.message; }
}
function renderShots() {
  const sb = state.storyboard;
  $("shots").innerHTML = sb.shots.map((s, i) => {
    const locked = state.locked.has(s.shot_number);
    return `
    <div class="card ${locked ? "locked" : ""}" id="shot_${s.shot_number}">
      <div class="shot-head">
        <span class="num">镜头 ${s.shot_number}</span>
        <span class="badge">${esc(s.shot_type)}</span>
        <span class="badge">${esc(String(s.duration_seconds))}s</span>
        ${locked ? '<span class="badge lock">🔒 已锁定</span>' : ""}
        <span class="shot-tools">
          <button class="small" onclick="editShot(${i})">编辑</button>
          <button class="small" onclick="moveShot(${i},-1)">上移</button>
          <button class="small" onclick="moveShot(${i},1)">下移</button>
          <button class="small" onclick="toggleLock(${s.shot_number})">${locked ? "解锁" : "锁定"}</button>
          <button class="small" onclick="regenShot(${s.shot_number})" ${locked ? "disabled" : ""}>重生成</button>
          <button class="small danger" onclick="delShot(${i})" ${locked ? "disabled" : ""}>删除</button>
        </span>
      </div>
      <div class="shot-body" id="shotbody_${i}">
        <div class="row"><span class="k">画面</span><span>${esc(s.scene_description)}</span></div>
        <div class="row"><span class="k">拍摄动作</span><span>${esc(s.shooting_action)}</span></div>
        <div class="row"><span class="k">运镜</span><span>${esc(s.camera_movement)}</span></div>
        <div class="row"><span class="k">旁白</span><span>${esc(s.voiceover)}</span></div>
        <div class="row"><span class="k">字幕</span><span>${esc(s.subtitle)}</span></div>
        <div class="row"><span class="k">道具</span><span>${esc((s.props || []).join("、"))}</span></div>
        <div class="row"><span class="k">素材</span><span>${esc((s.required_assets || []).join("、"))}</span></div>
        ${s.evidence_notes && s.evidence_notes !== "无" ? `<div class="row"><span class="k">依据</span><span>${esc(s.evidence_notes)}</span></div>` : ""}
      </div>
    </div>`;
  }).join("");
}
function editShot(i) {
  const s = state.storyboard.shots[i];
  const body = $("shotbody_" + i);
  body.innerHTML = `<div class="shot-edit">
    <label>时长（秒）<input id="e_dur_${i}" type="number" step="0.5" min="0.5" value="${esc(s.duration_seconds)}"></label>
    <label>景别<select id="e_type_${i}">${SHOT_TYPES.map((t) => `<option ${t === s.shot_type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>画面<textarea id="e_scene_${i}" rows="2">${esc(s.scene_description)}</textarea></label>
    <label>拍摄动作<textarea id="e_act_${i}" rows="2">${esc(s.shooting_action)}</textarea></label>
    <label>运镜<input id="e_cam_${i}" value="${esc(s.camera_movement)}"></label>
    <label>旁白<textarea id="e_vo_${i}" rows="2">${esc(s.voiceover)}</textarea></label>
    <label>字幕<input id="e_sub_${i}" value="${esc(s.subtitle)}"></label>
    <label>道具（顿号分隔）<input id="e_props_${i}" value="${esc((s.props || []).join("、"))}"></label>
    <label>素材（顿号分隔）<input id="e_assets_${i}" value="${esc((s.required_assets || []).join("、"))}"></label>
    <div class="actions">
      <button class="primary small" onclick="saveShot(${i})">保存</button>
      <button class="small" onclick="renderShots()">取消</button>
    </div></div>`;
}
async function saveShot(i) {
  const s = state.storyboard.shots[i];
  const split = (id) => $(id).value.split(/[、,，]/).map((x) => x.trim()).filter(Boolean);
  s.duration_seconds = parseFloat($("e_dur_" + i).value) || s.duration_seconds;
  s.shot_type = $("e_type_" + i).value;
  s.scene_description = $("e_scene_" + i).value;
  s.shooting_action = $("e_act_" + i).value;
  s.camera_movement = $("e_cam_" + i).value;
  s.voiceover = $("e_vo_" + i).value;
  s.subtitle = $("e_sub_" + i).value;
  s.props = split("e_props_" + i);
  s.required_assets = split("e_assets_" + i);
  renderShots();
  await refreshValidate();
}
/* 重排后按对象身份重建锁定集合（镜头号会变，对象引用不变） */
function remapLocked(lockedObjs) {
  const ns = new Set();
  state.storyboard.shots.forEach((sh) => { if (lockedObjs.has(sh)) ns.add(sh.shot_number); });
  state.locked = ns;
}
function delShot(i) {
  const s = state.storyboard.shots[i];
  if (state.locked.has(s.shot_number)) return;
  if (!confirm(`删除镜头 ${s.shot_number}？`)) return;
  const lockedObjs = new Set(
    state.storyboard.shots.filter((sh) => state.locked.has(sh.shot_number)));
  state.storyboard.shots.splice(i, 1);
  renumber();
  remapLocked(lockedObjs);
  renderShots(); refreshValidate();
}
function moveShot(i, dir) {
  const arr = state.storyboard.shots;
  const j = i + dir;
  if (j < 0 || j >= arr.length) return;
  const lockedObjs = new Set(arr.filter((sh) => state.locked.has(sh.shot_number)));
  [arr[i], arr[j]] = [arr[j], arr[i]];
  renumber();
  remapLocked(lockedObjs);
  renderShots(); refreshValidate();
}
function toggleLock(num) {
  if (state.locked.has(num)) state.locked.delete(num); else state.locked.add(num);
  renderShots();
}
async function regenShot(num) {
  const instruction = prompt(`对镜头 ${num} 的修改要求（例如：改成俯视角度）：`);
  if (!instruction) return;
  showErr("err3", "");
  try {
    setLoading(`正在重生成镜头 ${num}…`);
    const d = await api("/api/regenerate-shot", {
      storyboard: state.storyboard, shot_number: num,
      instruction, locked: [...state.locked],
    });
    state.storyboard = d.storyboard;
    if (d.warning) showErr("err3", "注意：" + d.warning);
    renderShots();
    await refreshValidate();
  } catch (e) { showErr("err3", e.message); }
  finally { setLoading(null); }
}
$("btn_back2").addEventListener("click", () => showView("v2"));
$("btn_goto_risk").addEventListener("click", () => { renderRiskView(); showView("v4"); });

/* ---------- 视图4：风险与导出 ---------- */
function renderRiskView() {
  const sb = state.storyboard;
  $("materials").innerHTML = (sb.materials_checklist || []).map((m) => `<li>${esc(m)}</li>`).join("") || "<li>无</li>";
  $("pendings").innerHTML = (sb.pending_confirmations || []).map((m) => `<li>${esc(m)}</li>`).join("") || "<li>无</li>";
  renderRisks();
}
function renderRisks() {
  if (!state.risks.length) { $("risks").innerHTML = '<p class="muted">尚未运行风险检查，或未发现风险。</p>'; return; }
  const lv = { "高": "high", "中": "mid", "低": "low" };
  $("risks").innerHTML = state.risks.map((f, i) => `
    <div class="card risk-item ${f.confirmed ? "confirmed" : ""}">
      <input type="checkbox" ${f.confirmed ? "checked" : ""} onchange="toggleConfirm(${i})" title="人工确认">
      <div>
        <div><span class="badge ${lv[f.level] || ""}">${esc(f.level)}风险</span>
        <span class="badge">${esc(f.source || "")}</span> ${esc(f.text)}</div>
        <div class="reason">${esc(f.reason || "")}</div>
      </div>
    </div>`).join("");
}
function toggleConfirm(i) {
  state.risks[i].confirmed = !state.risks[i].confirmed;
  state.storyboard.risk_flags = state.risks;
  renderRisks();
}
$("btn_risk").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在做三层风险检查…");
    const d = await api("/api/risk-check", {
      storyboard: state.storyboard,
      evidence_materials: state.product.evidence_materials,
      selling_points: state.product.selling_points,
      product: state.product,
    });
    state.risks = d.flags;
    state.storyboard.risk_flags = d.flags;
    renderRisks();
    if (!d.flags.length) $("risks").innerHTML = '<p class="muted">未发现明显风险提示（仍建议人工通读旁白/字幕复核）。</p>';
  } catch (e) { showErr("err4", e.message); }
  finally { setLoading(null); }
});
$("btn_back3").addEventListener("click", () => showView("v3"));
$("btn_export").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在生成 DOCX…");
    const r = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ storyboard: state.storyboard }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || "导出失败");
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "拍摄执行单.docx";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showErr("err4", e.message); }
  finally { setLoading(null); }
});
