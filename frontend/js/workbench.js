/* ============================================================
   V4: 工作台 UI
   ============================================================ */
function fillForm(p) {
  if (!p) return;
  $("f_name").value = p.product_name || "";
  $("f_price").value = p.price || "";
  $("f_desc").value = p.description || "";
  $("f_points").value = (p.selling_points || []).join("\n");
  $("f_audience").value = p.audience || "";
  $("f_evidence").value = p.evidence_materials || "";
  const s = p.shoot || {};
  $("f_device").value = s.device || "手机";
  $("f_people").value = s.people || 1;
  $("f_location").value = s.location || "";
  $("f_lighting").value = s.lighting || "";
  $("f_duration").value = s.target_duration_seconds || 60;
  $("f_note").value = s.note || "";
}

function getWorkProgress(data) {
  const d = data || {};
  const shots = d.storyboard && Array.isArray(d.storyboard.shots) ? d.storyboard.shots : [];
  const shootShots = d.shoot && d.shoot.shots ? Object.values(d.shoot.shots) : [];
  const captured = shootShots.filter((shot) => shot.status === "done").length;
  const checks = getStageState(d);
  let nextView = "v1", nextLabel = "填写产品档案";
  if (d.product) { nextView = "v1"; nextLabel = "补充产品事实或生成创意"; }
  if (d.ideas && d.ideas.length) { nextView = "v2"; nextLabel = "选择创意方向"; }
  if (shots.length) {
    if (!d.feasibility) { nextView = "v3"; nextLabel = "检查分镜与拍摄可行性"; }
    else if (captured < shots.length) { nextView = "v5"; nextLabel = "继续现场拍摄"; }
    else { nextView = "v4"; nextLabel = "审核风险并准备交付"; }
  }
  const completed = checks.filter(Boolean).length;
  return { percent: Math.round(completed / checks.length * 100), nextView, nextLabel, shotCount: shots.length, captured };
}

const WORKFLOW_STAGES = [
  { view: "v1", label: "产品档案" },
  { view: "v2", label: "创意方向" },
  { view: "v3", label: "分镜编辑" },
  { view: "v3-feasibility", label: "可行性检查" },
  { view: "v5", label: "拍摄执行" },
  { view: "v4", label: "风险审核与交付" },
];

function stageIndexForView(viewId, data) {
  if (viewId === "v1") return 0;
  if (viewId === "v2") return 1;
  if (viewId === "v3") return data && data.feasibility ? 3 : 2;
  if (viewId === "v5") return 4;
  if (viewId === "v4") return 5;
  return 0;
}

function getStageState(data) {
  const d = data || {};
  const shots = d.storyboard && Array.isArray(d.storyboard.shots) ? d.storyboard.shots : [];
  const shootShots = d.shoot && d.shoot.shots ? Object.values(d.shoot.shots) : [];
  const captured = shootShots.filter((shot) => shot.status === "done").length;
  const risks = d.risks || (d.storyboard && d.storyboard.risk_flags) || [];
  return [
    !!(d.product && d.product.product_name),
    !!(d.ideas && d.ideas.length),
    shots.length > 0,
    !!d.feasibility,
    shots.length > 0 && captured === shots.length,
    risks.length > 0 && risks.every((risk) => risk.confirmed || risk.level !== "高"),
  ];
}

function renderStageProgress(viewId) {
  const el = $("stage_progress");
  if (!el) return;
  const work = (works.items || []).find((item) => item.id === works.activeId);
  if (!work || viewId === "v0") { el.innerHTML = ""; el.classList.remove("visible"); return; }
  const states = getStageState(work.data || {});
  const index = stageIndexForView(viewId, work.data || {});
  const current = index < 0 ? 0 : index;
  el.classList.add("visible");
  el.innerHTML = `<div class="stage-progress-inner"><span class="stage-progress-kicker">当前作品</span><strong>${esc(work.name)}</strong><span class="stage-progress-divider"></span><span class="stage-progress-current">${String(current + 1).padStart(2, "0")} / ${String(WORKFLOW_STAGES.length).padStart(2, "0")} · ${WORKFLOW_STAGES[current].label}</span><div class="stage-progress-track"><span style="width:${Math.max(7, Math.round((current + 1) / WORKFLOW_STAGES.length * 100))}%"></span></div><div class="stage-progress-dots" aria-hidden="true">${WORKFLOW_STAGES.map((stage, i) => `<span class="${states[i] ? "done" : ""} ${i === current ? "current" : ""}"></span>`).join("")}</div></div>`;
}


function openWorkAtStage(id, view) {
  if (id !== works.activeId && !switchWork(id)) return;
  showView(view);
  renderWorks();
}

function renameWorkFromPrompt(id) {
  const work = works.items.find((item) => item.id === id);
  if (!work) return;
  const name = prompt("重命名作品", work.name);
  if (name) renameWork(id, name);
}

function renderOverviewMetrics() {
  const el = $("overview_metrics");
  if (!el) return;
  const items = works.items || [];
  const active = items.find((item) => item.id === works.activeId);
  const activeProgress = active ? getWorkProgress(active.data || {}) : { percent: 0, nextLabel: "新建作品后开始" };
  const completed = items.filter((item) => getWorkProgress(item.data || {}).percent === 100).length;
  const shooting = items.filter((item) => { const progress = getWorkProgress(item.data || {}); return progress.shotCount > 0 && progress.captured < progress.shotCount; }).length;
  const facts = items.reduce((total, item) => total + ((item.data || {}).facts || []).filter((fact) => fact.status === "confirmed").length, 0);
  const metrics = [
    { label: "我的作品", value: items.length, note: active ? `当前：${active.name}` : "从一份田野记录开始", tone: "green" },
    { label: "进行中的创作", value: shooting, note: shooting ? "有作品等待现场拍摄" : "暂无待拍摄作品", tone: "blue" },
    { label: "已确认事实", value: facts, note: "进入 AI 生成的可信依据", tone: "gold" },
    { label: "已完成作品", value: completed, note: active ? `当前作品 ${activeProgress.percent}% · ${activeProgress.nextLabel}` : "根据当前作品资料计算", tone: "coral" },
  ];
  el.innerHTML = metrics.map((metric) => `<div class="metric metric-${metric.tone}"><span class="metric-mark" aria-hidden="true"></span><div><span class="metric-label">${esc(metric.label)}</span><strong>${esc(metric.value)}</strong><p>${esc(metric.note)}</p></div></div>`).join("");
}

function renderWorks() {
  renderOverviewMetrics();
  const list = $("works_list");
  if (!works.items.length) {
    list.innerHTML = '<p class="muted">暂无作品，点击"新建作品"开始。</p>';
  } else {
    list.innerHTML = works.items.map((w, index) => {
      const d = w.data || {};
      const progress = getWorkProgress(d);
      const active = w.id === works.activeId;
      const safeId = esc(w.id);
      return `<article class="card work-card ${active ? "active-work" : ""}" style="--record-order:${Math.min(index, 8)}">
        <div class="work-head"><h4>${esc(w.name)}</h4>${active ? '<span class="badge lock">当前作品</span>' : ""}</div>
        <div class="work-summary"><span>${esc((d.product || {}).product_name || "产品资料待填写")}</span><span>${progress.shotCount ? `${progress.captured}/${progress.shotCount} 镜已拍` : "尚无分镜"}</span></div>
        <div class="work-progress"><div class="work-progress-label"><span>创作进度</span><strong>${progress.percent}%</strong></div><div class="progress-track" role="progressbar" aria-label="${esc(w.name)}创作进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.percent}"><span style="width:${progress.percent}%"></span></div></div>
        <p class="work-next"><span class="section-seal">下一步</span>${progress.nextLabel}</p>
        <div class="work-meta">更新于 ${new Date(w.updatedAt).toLocaleString()}</div>
        <div class="actions"><button class="primary small" onclick="openWorkAtStage('${safeId}','${progress.nextView}')">${active ? "继续创作" : "打开并继续"}</button><button class="ghost small" onclick="renameWorkFromPrompt('${safeId}')">重命名</button><button class="ghost small danger" onclick="deleteWork('${safeId}')">删除</button></div>
      </article>`;
    }).join("");
  }
  renderStagePanel();
}

function renderStagePanel() {
  const w = works.items.find((x) => x.id === works.activeId);
  const panel = $("stage_panel");
  if (!w) { panel.innerHTML = ""; return; }
  const d = w.data || {};
  const progress = getWorkProgress(d);
  const currentStage = stageIndexForView(progress.nextView, d);
  const stages = [
    { label: "产品资料", done: !!d.product },
    { label: "创意方案", done: !!(d.ideas && d.ideas.length) },
    { label: "分镜编辑", done: !!(d.storyboard && d.storyboard.shots && d.storyboard.shots.length) },
    { label: "可行性检查", done: !!d.feasibility },
    { label: "现场拍摄", done: progress.shotCount > 0 && progress.captured === progress.shotCount },
    { label: "风险审核与交付", done: false },
  ];
  panel.innerHTML = `<div class="stage-panel-heading"><div><span class="section-seal">创作轨道</span><h3>当前推进</h3></div><strong title="${esc(w.name)}">${esc(w.name)}</strong></div><div class="stage-flow" role="list">${stages.map((s, index) => { const current = index === currentStage; return `<span class="stage-chip ${s.done ? "done" : ""} ${current ? "current" : ""}" role="listitem" ${current ? 'aria-current="step"' : ""}><span class="stage-node">${s.done ? "✓" : String(index + 1).padStart(2, "0")}</span><span>${s.label}</span></span>`; }).join("")}<span class="stage-flow-progress" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, Math.round(currentStage / (stages.length - 1) * 100)))}%"></i></span></div><div class="stage-panel-footer"><p><span class="stage-next-label">建议动作</span><strong>${esc(progress.nextLabel)}</strong></p><button class="primary small" onclick="showView('${progress.nextView}')">继续创作</button></div>`;
}

function renderAll() {
  renderWorks();
  if (state.storyboard) { renderShots(); refreshValidate(); }
  if (state.ideas && state.ideas.length) renderIdeas();
  renderRisks();
}

const _origRenderFeasibility = typeof renderFeasibility !== "undefined" ? renderFeasibility : null;

function checkFeasibilityStale() {
  const panel = $("feasibility_panel");
  if (!panel || panel.classList.contains("hidden")) return;
  if (isFeasibilityStale()) {
    const notice = document.createElement("p");
    notice.className = "warn-text";
    notice.id = "feas_stale_notice";
    if (!$("feas_stale_notice")) {
      notice.textContent = "⚠ 分镜已修改，此检查结果可能已过期，建议重新检查。";
      panel.prepend(notice);
    }
  } else {
    const n = $("feas_stale_notice");
    if (n) n.remove();
  }
}

$("btn_new_work").addEventListener("click", () => {
  const name = prompt("请输入作品名称：", "作品 " + (works.items.length + 1));
  if (name === null) return;
  createWork(name.trim() || undefined);
  showView("v1");
});

(function initV4() {
  loadWorks();
  migrateLegacyShoot();
  if (!works.items.length) createWork("我的第一个作品");
  else if (works.activeId) {
    const w = works.items.find((x) => x.id === works.activeId);
    if (w) applyWorkData(w.data);
  } else {
    works.activeId = works.items[0].id;
    applyWorkData(works.items[0].data);
  }
  showView("v0");
})();

function saveCustomNote(num) {
  const input = $("note_input_" + num);
  if (!input) return;
  const val = input.value.trim().slice(0, 200);
  setReshootNote(num, val);
  renderShootMode();
  const btn = event && event.target;
  if (btn) { const t = btn.textContent; btn.textContent = "✓ 已保存"; setTimeout(() => btn.textContent = t, 1500); }
}

$("btn_api_settings").addEventListener("click", () => {
  const panel = $("api_settings_panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    const cfg = getApiConfig();
    $("cfg_base").value = cfg.base;
    $("cfg_key").value = "";
    $("cfg_model").value = cfg.model;
    updateCfgStatus();
  }
});
$("btn_save_cfg").addEventListener("click", () => {
  const base = $("cfg_base").value.trim();
  const key = $("cfg_key").value.trim();
  const model = $("cfg_model").value.trim();
  if (base && !/^https?:\/\//i.test(base)) { alert("API Base URL 必须以 http:// 或 https:// 开头"); return; }
  if (setApiConfig(base, key, model)) {
    $("cfg_key").value = "";
    updateCfgStatus();
    alert(key ? "已保存到当前会话（关闭标签页后清除）" : "已更新配置");
  } else alert("保存失败：浏览器存储不可用");
});
$("btn_clear_cfg").addEventListener("click", () => {
  if (!confirm("确定清除当前会话的 API 配置吗？")) return;
  clearApiConfig();
  $("cfg_base").value = "";
  $("cfg_key").value = "";
  $("cfg_model").value = "";
  updateCfgStatus();
});
