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
  // S7.4：审核过期必须明显标记，绝不展示为"通过"
  const expiryNotice = (window.LPExpiry && state.reviewMeta)
    ? window.LPExpiry.notice(state.reviewMeta) : "";
  $("risks").innerHTML = expiryNotice + state.risks.map((f, i) => `
    <div class="card risk-item ${f.confirmed ? "confirmed" : ""}">
      <input type="checkbox" ${f.confirmed ? "checked" : ""} onchange="toggleConfirm(${i})" title="人工确认">
      <div>
        <div><span class="badge ${lv[f.level] || ""}">${esc(f.level)}风险</span>
        <span class="badge">${esc(f.source || "")}</span> ${esc(f.text)}</div>
        <div class="reason">${esc(f.reason || "")}</div>
      </div>
    </div>`).join("");
  // 过期时给整个风险区加样式
  if (window.LPExpiry && window.LPExpiry.isExpired(state.reviewMeta)) {
    $("risks").classList.add("lp-review-expired");
  } else {
    $("risks").classList.remove("lp-review-expired");
  }
}
function toggleConfirm(i) {
  // 005: 每次勾选/撤销都走统一更新入口，自动触发保存
  updateState((st) => {
    st.risks[i].confirmed = !st.risks[i].confirmed;
    if (st.storyboard) st.storyboard.risk_flags = st.risks;
  });
  renderRisks();
}
$("btn_risk").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在做三层风险检查…");
    const d = await api("/api/risk-check", {
      storyboard: stripUids(state.storyboard),
      evidence_materials: state.product.evidence_materials,
      selling_points: state.product.selling_points,
      product: state.product,
      facts: state.facts || [],
    }, { signal: workSignal() });
    state.risks = d.flags;
    state.storyboard.risk_flags = d.flags;
    // S7.4：后端如返回审核新鲜度信息（review_meta），过期时标记
    state.reviewMeta = d.review_meta || d.review || null;
    if (typeof scheduleSave === "function") scheduleSave();
    renderRisks();
    if (!d.flags.length) $("risks").innerHTML = '<p class="muted">未发现明显风险提示（仍建议人工通读旁白/字幕复核）。</p>';
  } catch (e) { if (isAbortError(e)) return; showErr("err4", e.message); }
  finally { setLoading(null); }
});
$("btn_back3").addEventListener("click", () => showView("v3"));
$("btn_export").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在生成 DOCX…");
    const r = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ storyboard: stripUids(state.storyboard) }),
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


function renderDeliveryPanel(result) {
  const panel = $("delivery_panel");
  panel.classList.remove("hidden");
  const labels = { done: "已完成", action: "需要处理", suggest: "建议完善", na: "不适用" };
  // S7.4：交付检查结果过期时明显标记，不允许展示为"可以交付"
  let expiredHtml = "";
  const reviewInfo = result.review_meta || result.review || null;
  if (window.LPExpiry && window.LPExpiry.isExpired(reviewInfo)) {
    expiredHtml = window.LPExpiry.notice(reviewInfo);
    // 过期则强制视为不可交付（覆盖 result.ready）
    result = Object.assign({}, result, { ready: false });
  }
  panel.innerHTML = expiredHtml + `<h3>${result.ready ? "✓ 可以交付" : "尚不能交付"}</h3>` + result.items.map((item) => `<div class="delivery-item ${item.status}"><strong>${esc(item.label)}</strong><span>${esc(labels[item.status] || item.status)}</span><p>${esc(item.reason)}</p></div>`).join("");
}
$("btn_delivery_check").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在检查交付状态…");
    const result = await api("/api/delivery-check", { storyboard: stripUids(state.storyboard), product: state.product, facts: state.facts || [], feasibility: state.feasibility || null, shoot_state: state.shoot || {} }, { signal: workSignal() });
    renderDeliveryPanel(result);
  } catch (e) { if (isAbortError(e)) return; showErr("err4", e.message); } finally { setLoading(null); }
});
$("btn_export_srt").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在生成 SRT 字幕…");
    const r = await fetch("/api/export-srt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ storyboard: stripUids(state.storyboard) }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || "SRT 导出失败"); }
    const blob = await r.blob(); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "字幕.srt"; a.click(); URL.revokeObjectURL(a.href);
  } catch (e) { showErr("err4", e.message); } finally { setLoading(null); }
});
