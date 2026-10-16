/* ---------- 视图3：分镜编辑器 ---------- */
function renumber() {
  state.storyboard.shots.forEach((s, i) => { s.shot_number = i + 1; });
}
async function refreshValidate() {
  const bar = $("validate_bar");
  try {
    const d = await api("/api/validate", { storyboard: stripUids(state.storyboard) });
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
  // 分镜变化后检查可行性报告是否过期
  if (typeof checkFeasibilityStale === 'function') setTimeout(checkFeasibilityStale, 100);
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
        ${s.fact_refs && s.fact_refs.length ? `<div class="row"><span class="k">事实依据</span><span>${esc(s.fact_refs.map((id) => factLabel(id)).join("、"))}</span></div>` : ""}
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
  markDirty();
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
  markDirty();
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
  markDirty();
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
  markDirty();
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
      storyboard: stripUids(state.storyboard), shot_number: num,
      instruction, locked: [...state.locked],
    });
    state.storyboard = d.storyboard;
    if (typeof assignShotUids === 'function') assignShotUids(state.storyboard);
    if (d.warning) showErr("err3", "注意：" + d.warning);
    renderShots();
    await refreshValidate();
  } catch (e) { showErr("err3", e.message); }
  finally { setLoading(null); }
}
$("btn_back2").addEventListener("click", () => showView("v2"));
$("btn_goto_risk").addEventListener("click", () => { renderRiskView(); showView("v4"); });

