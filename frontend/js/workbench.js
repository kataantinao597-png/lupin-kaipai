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

function renderWorks() {
  const list = $("works_list");
  if (!works.items.length) {
    list.innerHTML = '<p class="muted">暂无作品，点击"新建作品"开始。</p>';
  } else {
    list.innerHTML = works.items.map((w) => {
      const d = w.data || {};
      const sb = d.storyboard;
      const shots = (sb && sb.shots ? sb.shots.length : 0);
      const active = w.id === works.activeId;
      return `<div class="card ${active ? "active-work" : ""}">
        <div class="shot-head">
          <span class="num">${esc(w.name)}</span>
          ${active ? '<span class="badge">当前</span>' : ""}
        </div>
        <div class="row"><span class="k">产品</span><span>${esc((d.product || {}).product_name || "未填写")}</span></div>
        <div class="row"><span class="k">分镜</span><span>${shots ? shots + " 镜头" : "未生成"}</span></div>
        <div class="row"><span class="k">更新</span><span>${new Date(w.updatedAt).toLocaleString()}</span></div>
        <div class="actions">
          ${active ? "" : `<button class="primary small" onclick="switchWork('${w.id}');showView('v1')">打开</button>`}
          <button class="ghost small" onclick="const n=prompt('重命名作品', '${esc(w.name)}'); if(n) renameWork('${w.id}', n)">重命名</button>
          <button class="ghost small danger" onclick="deleteWork('${w.id}')">删除</button>
        </div>
      </div>`;
    }).join("");
  }
  renderStagePanel();
}

function renderStagePanel() {
  const w = works.items.find((x) => x.id === works.activeId);
  const panel = $("stage_panel");
  if (!w) { panel.innerHTML = ""; return; }
  const d = w.data || {};
  const stages = [
    { id: "v1", label: "产品资料", done: !!d.product },
    { id: "v2", label: "创意方案", done: !!(d.ideas && d.ideas.length) },
    { id: "v3", label: "分镜编辑", done: !!(d.storyboard && d.storyboard.shots && d.storyboard.shots.length) },
    { id: "feas", label: "可行性检查", done: !!d.feasibility },
    { id: "v5", label: "现场拍摄", done: false },
    { id: "v4", label: "交付", done: false },
  ];
  // 现场拍摄完成度
  if (d.storyboard && d.shoot) {
    const total = d.storyboard.shots.length;
    const done = Object.values(d.shoot.shots || {}).filter((s) => s.status === "done").length;
    stages[4].done = total > 0 && done === total;
  }
  panel.innerHTML = `<h3>当前作品：${esc(w.name)} — 阶段进度</h3>
    <div class="stage-flow">${stages.map((s) =>
      `<span class="stage-chip ${s.done ? "done" : ""}">${s.done ? "✓ " : ""}${s.label}</span>`
    ).join('<span class="stage-arrow">→</span>')}</div>
    <p class="muted">阶段状态根据真实数据计算。下一步：${nextAction(d)}</p>`;
}

function nextAction(d) {
  if (!d.product) return "填写产品资料";
  if (!d.ideas || !d.ideas.length) return "生成创意方案";
  if (!d.storyboard) return "选择创意并生成分镜";
  if (!d.feasibility) return "运行拍摄可行性检查";
  return "进入现场拍摄或导出文档";
}

function renderAll() {
  renderWorks();
  if (state.storyboard) { renderShots(); refreshValidate(); }
  if (state.ideas && state.ideas.length) renderIdeas();
  renderRisks();
}

// 可行性过期标记：在 renderFeasibility 中检查
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

// 初始化
$("btn_new_work").addEventListener("click", () => {
  const name = prompt("请输入作品名称：", "作品 " + (works.items.length + 1));
  if (name === null) return;
  createWork(name.trim() || undefined);
  showView("v1");
});

// 页面加载时初始化
(function initV4() {
  loadWorks();
  migrateLegacyShoot();
  if (!works.items.length) {
    createWork("我的第一个作品");
  } else if (works.activeId) {
    const w = works.items.find((x) => x.id === works.activeId);
    if (w) applyWorkData(w.data);
  } else {
    works.activeId = works.items[0].id;
    applyWorkData(works.items[0].data);
  }
  // 为现有操作添加自动保存
  const origSetShootStatus = typeof setShootStatus !== "undefined" ? setShootStatus : null;
  showView("v0");
})();

function saveCustomNote(num) {
  const input = $("note_input_" + num);
  if (!input) return;
  const val = input.value.trim().slice(0, 200);
  setReshootNote(num, val);
  renderShootMode();
  // 反馈
  const btn = event && event.target;
  if (btn) { const t = btn.textContent; btn.textContent = "✓ 已保存"; setTimeout(() => btn.textContent = t, 1500); }
}


/* ---------- API 配置面板 ---------- */
$("btn_api_settings").addEventListener("click", () => {
  const panel = $("api_settings_panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    const cfg = getApiConfig();
    $("cfg_base").value = cfg.base;
    $("cfg_key").value = "";  // 不回显 Key
    $("cfg_model").value = cfg.model;
    updateCfgStatus();
  }
});
$("btn_save_cfg").addEventListener("click", () => {
  const base = $("cfg_base").value.trim();
  const key = $("cfg_key").value.trim();
  const model = $("cfg_model").value.trim();
  if (base && !/^https?:\/\//i.test(base)) {
    alert("API Base URL 必须以 http:// 或 https:// 开头");
    return;
  }
  if (setApiConfig(base, key, model)) {
    $("cfg_key").value = "";
    updateCfgStatus();
    alert(key ? "已保存到当前会话（关闭标签页后清除）" : "已更新配置");
  } else {
    alert("保存失败：浏览器存储不可用");
  }
});
$("btn_clear_cfg").addEventListener("click", () => {
  if (!confirm("确定清除当前会话的 API 配置吗？")) return;
  clearApiConfig();
  $("cfg_base").value = "";
  $("cfg_key").value = "";
  $("cfg_model").value = "";
  updateCfgStatus();
});
