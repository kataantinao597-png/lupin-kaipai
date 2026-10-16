/* ---------- V3: 现场拍摄模式 ---------- */
const SHOOT_VERSION = 1;
const SHOOT_STATUS = { todo: "待拍摄", done: "已拍摄", reshoot: "需要补拍" };
const RESHOOT_REASONS = ["对焦失败", "画面抖动", "产品细节没拍清楚", "口播出错", "环境声音影响收音"];
state.shoot = { key: "", shots: {} };
state.shootFilter = "all";

function shootKey(sb) {
  // 用产品名+镜头数+各镜头号+时长生成 key，避免错配
  const parts = [sb.product_summary || "", (sb.shots || []).length];
  (sb.shots || []).forEach((s) => parts.push(s.shot_number + ":" + s.duration_seconds));
  let h = 0;
  const str = parts.join("|");
  for (let i = 0; i < str.length; i++) { h = ((h << 5) - h + str.charCodeAt(i)) | 0; }
  return "lupin_shoot_v" + SHOOT_VERSION + "_" + (h >>> 0).toString(36);
}

function loadShootState() {
  const sb = state.storyboard;
  if (!sb || !sb.shots) return;
  const key = shootKey(sb);
  state.shoot.key = key;
  state.shoot.shots = {};
  try {
    const raw = localStorage.getItem(key);
    if (raw) {
      const data = JSON.parse(raw);
      if (data.version === SHOOT_VERSION && data.shots) {
        // 只保留当前分镜中存在的镜头号
        const validNums = new Set(sb.shots.map((s) => String(s.shot_number)));
        Object.keys(data.shots).forEach((num) => {
          if (validNums.has(num) && SHOOT_STATUS[data.shots[num].status]) {
            state.shoot.shots[num] = {
              status: data.shots[num].status,
              note: String(data.shots[num].note || "").slice(0, 200),
            };
          }
        });
      }
    }
  } catch (e) { /* 存储不可用时静默降级，不影响主功能 */ }
}

function saveShootState() {
  try {
    localStorage.setItem(state.shoot.key, JSON.stringify({
      version: SHOOT_VERSION,
      shots: state.shoot.shots,
      savedAt: Date.now(),
    }));
  } catch (e) { /* 静默降级 */ }
}

function getShootStatus(num) {
  return (state.shoot.shots[String(num)] || {}).status || "todo";
}

function setShootStatus(num, status) {
  const key = String(num);
  if (!state.shoot.shots[key]) state.shoot.shots[key] = { status: "todo", note: "" };
  state.shoot.shots[key].status = status;
  if (status !== "reshoot") state.shoot.shots[key].note = "";
  saveShootState();
  renderShootMode();
}

function setReshootNote(num, note) {
  const key = String(num);
  if (!state.shoot.shots[key]) state.shoot.shots[key] = { status: "reshoot", note: "" };
  state.shoot.shots[key].note = String(note).slice(0, 200);
  saveShootState();
}

function shootStats() {
  const shots = (state.storyboard && state.storyboard.shots) || [];
  const total = shots.length;
  let done = 0, reshoot = 0;
  shots.forEach((s) => {
    const st = getShootStatus(s.shot_number);
    if (st === "done") done++;
    else if (st === "reshoot") reshoot++;
  });
  const todo = total - done - reshoot;
  const pct = total ? Math.round((done / total) * 100) : 0;
  return { total, done, todo, reshoot, pct };
}

function renderShootMode() {
  const sb = state.storyboard;
  if (!sb || !sb.shots) {
    $("shoot_cards").innerHTML = '<p class="muted">暂无分镜，请先生成分镜。</p>';
    return;
  }
  loadShootState(); // 确保 key 匹配当前分镜
  const st = shootStats();
  $("shoot_meta").textContent = `${sb.product_summary || ""} · 目标${sb.target_duration_seconds || ""}s`;
  $("shoot_stats").innerHTML = `
    <div class="stat"><span class="stat-num">${st.total}</span><span class="stat-label">总镜头</span></div>
    <div class="stat"><span class="stat-num">${st.done}</span><span class="stat-label">已拍摄</span></div>
    <div class="stat"><span class="stat-num">${st.todo}</span><span class="stat-label">待拍摄</span></div>
    <div class="stat"><span class="stat-num">${st.reshoot}</span><span class="stat-label">需补拍</span></div>
    <div class="stat"><span class="stat-num">${st.pct}%</span><span class="stat-label">已完成</span></div>`;
  const filter = state.shootFilter;
  const visible = sb.shots.filter((s) => {
    const cur = getShootStatus(s.shot_number);
    if (filter === "all") return true;
    if (filter === "todo_only") return cur !== "done";
    return cur === filter;
  });
  $("shoot_cards").innerHTML = visible.map((s) => {
    const cur = getShootStatus(s.shot_number);
    const note = (state.shoot.shots[String(s.shot_number)] || {}).note || "";
    const locked = state.locked.has(s.shot_number);
    const fields = [];
    if (s.scene_description) fields.push(`<div class="row"><span class="k">画面</span><span>${esc(s.scene_description)}</span></div>`);
    if (s.shooting_action) fields.push(`<div class="row"><span class="k">拍摄动作</span><span>${esc(s.shooting_action)}</span></div>`);
    if (s.camera_movement) fields.push(`<div class="row"><span class="k">机位</span><span>${esc(s.camera_movement)}</span></div>`);
    const props = [...(s.props || []), ...(s.required_assets || [])];
    if (props.length) fields.push(`<div class="row"><span class="k">道具</span><span>${esc(props.join("、"))}</span></div>`);
    if (s.voiceover) fields.push(`<details><summary>口播（点击展开）</summary><p>${esc(s.voiceover)}</p></details>`);
    if (s.subtitle) fields.push(`<div class="row"><span class="k">字幕</span><span>${esc(s.subtitle)}</span></div>`);
    return `<div class="card shoot-card status-${cur}">
      <div class="shot-head">
        <span class="num">镜头 ${s.shot_number}</span>
        <span class="badge">${s.duration_seconds}s</span>
        <span class="badge status">${SHOOT_STATUS[cur]}</span>
        ${locked ? '<span class="badge lock">🔒 已锁定</span>' : ""}
      </div>
      ${fields.join("")}
      <div class="shoot-actions">
        ${["todo", "done", "reshoot"].map((k) =>
          `<button class="small ${cur === k ? "primary" : "ghost"}" onclick="setShootStatus(${s.shot_number},'${k}')">${SHOOT_STATUS[k]}</button>`
        ).join("")}
      </div>
      ${cur === "reshoot" ? `<div class="reshoot-note">
        <label>补拍原因</label>
        <div class="note-presets">
          ${RESHOOT_REASONS.map((r) => `<button class="small ghost ${note === r ? "primary" : ""}" onclick="setReshootNote(${s.shot_number}, '${r}'); renderShootMode();">${r}</button>`).join("")}
        </div>
        <div class="note-input-row">
          <input id="note_input_${s.shot_number}" placeholder="或输入自定义原因…" value="${RESHOOT_REASONS.includes(note) ? "" : esc(note)}" maxlength="200">
          <button class="small primary" onclick="saveCustomNote(${s.shot_number})">保存</button>
          ${note ? `<button class="small ghost" onclick="setReshootNote(${s.shot_number}, ''); renderShootMode();">清空</button>` : ""}
        </div>
        ${note ? `<div class="row"><span class="k">当前备注</span><span>${esc(note)}</span></div>` : ""}
      </div>` : ""}
    </div>`;
  }).join("") || '<p class="muted">该筛选下无镜头。</p>';
}

$("btn_shoot_mode").addEventListener("click", () => {
  if (!state.storyboard || !state.storyboard.shots) { showErr("err3", "请先生成分镜"); return; }
  showErr("err3", "");
  loadShootState();
  state.shootFilter = "all";
  document.querySelectorAll(".filter-btn").forEach((b) => b.classList.toggle("active", b.dataset.filter === "all"));
  renderShootMode();
  showView("v5");
});
$("btn_back_shoot").addEventListener("click", () => showView("v3"));
document.querySelectorAll(".filter-btn").forEach((b) => b.addEventListener("click", () => {
  state.shootFilter = b.dataset.filter;
  document.querySelectorAll(".filter-btn").forEach((x) => x.classList.toggle("active", x === b));
  renderShootMode();
}));
$("btn_filter_todo_only").addEventListener("click", () => {
  state.shootFilter = "todo_only";
  document.querySelectorAll(".filter-btn").forEach((x) => x.classList.remove("active"));
  renderShootMode();
});
$("btn_export_checklist").addEventListener("click", async () => {
  showErr("err5", "");
  try {
    setLoading("正在生成拍摄清单…");
    const r = await fetch("/api/shooting-checklist", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ storyboard: stripUids(state.storyboard), shoot_state: state.shoot }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || "导出失败");
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "现场拍摄清单.docx";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { showErr("err5", e.message); }
  finally { setLoading(null); }
});

