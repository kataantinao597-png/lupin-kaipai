/* ---------- 拍摄可行性检查 ---------- */
const FEAS_COLORS = { "可执行": "ok", "建议调整": "warn", "不可执行": "bad", "需要人工确认": "muted" };

$("btn_feasibility").addEventListener("click", async () => {
  showErr("err3", "");
  try {
    setLoading("正在检查拍摄可行性…");
    const d = await api("/api/feasibility-check", { storyboard: stripUids(state.storyboard) });
    state.feasibility = d.results;
    state.feasibilityHash = storyboardHash(state.storyboard);
    renderFeasibility(d);
    if (typeof scheduleSave === "function") scheduleSave();
  } catch (e) { showErr("err3", e.message); }
  finally { setLoading(null); }
});

function renderFeasibility(d) {
  const panel = $("feasibility_panel");
  panel.classList.remove("hidden");
  const counts = {};
  d.results.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
  const total = d.results.length;
  const summary = ["可执行", "建议调整", "不可执行", "需要人工确认"]
    .map((s) => `${s} ${counts[s] || 0}`)
    .join(" · ");
  panel.innerHTML = `
    <h3>拍摄可行性检查 <span class="muted">（共 ${total} 镜头：${summary}）</span></h3>
    ${d.model_completed ? "" : `<p class="warn-text">⚠ ${esc(d.notice || "模型检查未完成")}</p>`}
    <p class="muted">本检查为拍摄可行性提示，不构成绝对保证；最终以实际拍摄条件为准。</p>
    ${d.results.map((r) => {
      const locked = state.locked.has(r.shot_number);
      const alt = r.alternative;
      return `<div class="card feas-${FEAS_COLORS[r.status] || "muted"}">
        <div class="shot-head">
          <span class="num">镜头 ${r.shot_number}</span>
          <span class="badge feas">${esc(r.status)}</span>
          <span class="badge">${esc(r.source)}判断</span>
          ${locked ? '<span class="badge lock">🔒 已锁定</span>' : ""}
        </div>
        <div class="row"><span class="k">原因</span><span>${esc(r.reason)}</span></div>
        ${r.constraints_involved.length ? `<div class="row"><span class="k">涉及约束</span><span>${esc(r.constraints_involved.join("、"))}</span></div>` : ""}
        ${alt ? `<div class="alt">
          <div class="row"><span class="k">替代拍摄方式</span><span>${esc(alt.shooting_method)}</span></div>
          <div class="row"><span class="k">设备用法</span><span>${esc(alt.device_usage)}</span></div>
          <div class="row"><span class="k">机位角度</span><span>${esc(alt.camera_position)}</span></div>
          <div class="row"><span class="k">人物动作</span><span>${esc(alt.action)}</span></div>
          ${alt.props_needed.length ? `<div class="row"><span class="k">道具场地</span><span>${esc(alt.props_needed.join("、"))}</span></div>` : ""}
          <div class="row"><span class="k">建议时长</span><span>${esc(String(alt.suggested_duration))}s</span></div>
          <div class="row"><span class="k">保留目的</span><span>${esc(alt.preserved_purpose)}</span></div>
          <div class="actions">
            ${locked
              ? `<span class="muted">该镜头已锁定，请先解除锁定再应用替代方案</span>`
              : `<button class="primary small" onclick="applyAlternative(${r.shot_number})">采用替代方案</button>`}
          </div>
        </div>` : ""}
      </div>`;
    }).join("")}`;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function applyAlternative(num) {
  const feas = (state.feasibility || []).find((r) => r.shot_number === num);
  if (!feas || !feas.alternative) return;
  if (state.locked.has(num)) {
    alert(`镜头 ${num} 已锁定，请先解除锁定再应用替代方案。`);
    return;
  }
  const alt = feas.alternative;
  const shot = state.storyboard.shots.find((s) => s.shot_number === num);
  if (!shot) return;
  const oldDur = shot.duration_seconds;
  // 只修改当前镜头：拍摄动作/运镜/画面/道具/素材/时长
  shot.shooting_action = `${alt.shooting_method}。具体：${alt.device_usage}`;
  shot.camera_movement = alt.camera_position;
  shot.scene_description = `${alt.action}`;
  if (alt.props_needed.length) shot.props = alt.props_needed;
  const newDur = parseFloat(alt.suggested_duration) || oldDur;
  if (Math.abs(newDur - oldDur) > 0.01) {
    if (!confirm(`替代方案建议时长 ${newDur}s（原 ${oldDur}s），将改变总时长。是否继续？`)) return;
    shot.duration_seconds = newDur;
  }
  // 标记该镜头已处理，避免重复提示（走统一更新入口，自动保存+关联失效）
  updateState((st) => {
    feas.status = "可执行";
    feas.reason = "已采用替代方案";
    feas.alternative = null;
  }, { storyboardChanged: true });
  // V4: 替代方案改变了拍摄内容，提示用户确认拍摄状态
  const recKey = (typeof recKeyFor === "function") ? recKeyFor(num) : String(num);
  const shootSt = (state.shoot.shots[recKey] || {}).status;
  if (shootSt === "done") {
    if (confirm(`镜头 ${num} 已标记为"已拍摄"，应用替代方案后建议重新拍摄。是否将其改回"待拍摄"？`)) {
      state.shoot.shots[recKey].status = "todo";
      state.shoot.shots[recKey].note = "";
      if (typeof saveShootState === "function") saveShootState();
    }
  }
  renderShots();
  await refreshValidate();
  renderFeasibility({ results: state.feasibility, model_completed: true });
}

