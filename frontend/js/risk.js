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
      storyboard: stripUids(state.storyboard),
      evidence_materials: state.product.evidence_materials,
      selling_points: state.product.selling_points,
      product: state.product,
      facts: state.facts || [],
    });
    state.risks = d.flags;
    state.storyboard.risk_flags = d.flags;
    if (typeof scheduleSave === "function") scheduleSave();
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
  panel.innerHTML = `<h3>${result.ready ? "✓ 可以交付" : "尚不能交付"}</h3>` + result.items.map((item) => `<div class="delivery-item ${item.status}"><strong>${esc(item.label)}</strong><span>${esc(labels[item.status] || item.status)}</span><p>${esc(item.reason)}</p></div>`).join("");
}
$("btn_delivery_check").addEventListener("click", async () => {
  showErr("err4", "");
  try {
    setLoading("正在检查交付状态…");
    const result = await api("/api/delivery-check", { storyboard: stripUids(state.storyboard), product: state.product, facts: state.facts || [], feasibility: state.feasibility || null, shoot_state: state.shoot || {} });
    renderDeliveryPanel(result);
  } catch (e) { showErr("err4", e.message); } finally { setLoading(null); }
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
