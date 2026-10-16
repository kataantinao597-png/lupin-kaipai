/* ============================================================
   V5: 产品事实库
   ============================================================ */
const FACT_STATUS_LABEL = { confirmed: "已确认", pending: "待核实" };
let editingFactId = null;

function factsHash() {
  const usable = (state.facts || []).filter((f) => f.allow_use && f.status === "confirmed");
  return usable.map((f) => f.id + ":" + f.content).join("|");
}

function usableFacts() {
  return (state.facts || []).filter((f) => f.allow_use && f.status === "confirmed");
}

function renderFacts() {
  const list = $("facts_list");
  if (!list) return;
  const facts = state.facts || [];
  if (!facts.length) {
    list.innerHTML = '<p class="muted">暂无事实。添加已确认的产品信息，AI 生成时将优先使用。</p>';
    return;
  }
  list.innerHTML = facts.map((f) => `
    <div class="card fact-card ${f.status}">
      <div class="shot-head">
        <span class="badge">${esc(f.category)}</span>
        <span class="badge ${f.status}">${FACT_STATUS_LABEL[f.status]}</span>
        ${f.allow_use ? "" : '<span class="badge muted">不用于生成</span>'}
      </div>
      <p>${esc(f.content)}</p>
      ${f.source ? `<div class="row"><span class="k">来源</span><span>${esc(f.source)}</span></div>` : ""}
      <div class="actions">
        <button class="small ghost" onclick="editFact('${f.id}')">编辑</button>
        ${f.status === "pending" ? `<button class="small primary" onclick="confirmFact('${f.id}')">确认</button>` : ""}
        <button class="small ghost danger" onclick="deleteFact('${f.id}')">删除</button>
      </div>
    </div>`).join("");
}

function editFact(id) {
  const f = (state.facts || []).find((x) => x.id === id);
  if (!f) return;
  editingFactId = id;
  $("fact_editor_title").textContent = "编辑事实";
  $("fact_content").value = f.content;
  $("fact_category").value = f.category;
  $("fact_status").value = f.status;
  $("fact_source").value = f.source || "";
  $("fact_allow").checked = f.allow_use;
  $("fact_editor").classList.remove("hidden");
}

function confirmFact(id) {
  const f = (state.facts || []).find((x) => x.id === id);
  if (f) { f.status = "confirmed"; onFactsChanged(); }
}

function deleteFact(id) {
  if (!confirm("确定删除这条事实吗？")) return;
  state.facts = (state.facts || []).filter((x) => x.id !== id);
  onFactsChanged();
}

function onFactsChanged() {
  state.factsUpdatedAt = Date.now();
  renderFacts();
  if (typeof scheduleSave === "function") scheduleSave();
  checkFactsStale();
}

function checkFactsStale() {
  // 事实更新后，标记旧创意/分镜可能过期
  const h = factsHash();
  ["ideas_stale", "sb_stale"].forEach((id) => { const el = $(id); if (el) el.remove(); });
  if (state.ideas && state.ideas.length && state.ideasFactsHash && h !== state.ideasFactsHash) {
    const v2 = $("ideas");
    if (v2) { const p = document.createElement("p"); p.id = "ideas_stale"; p.className = "warn-text"; p.textContent = "⚠ 产品事实已更新，这些创意可能基于旧资料，建议重新生成。"; v2.prepend(p); }
  }
  if (state.storyboard && state.sbFactsHash && h !== state.sbFactsHash) {
    const v3 = $("shots");
    if (v3) { const p = document.createElement("p"); p.id = "sb_stale"; p.className = "warn-text"; p.textContent = "⚠ 产品事实已更新，此分镜可能基于旧资料。"; v3.prepend(p); }
  }
}

$("btn_add_fact").addEventListener("click", () => {
  editingFactId = null;
  $("fact_editor_title").textContent = "添加事实";
  $("fact_content").value = "";
  $("fact_category").value = "产地";
  $("fact_status").value = "confirmed";
  $("fact_source").value = "";
  $("fact_allow").checked = true;
  $("fact_editor").classList.remove("hidden");
});
$("btn_cancel_fact").addEventListener("click", () => {
  $("fact_editor").classList.add("hidden");
  editingFactId = null;
});
$("btn_save_fact").addEventListener("click", () => {
  const content = $("fact_content").value.trim();
  if (!content) { alert("请填写事实内容"); return; }
  const data = {
    content,
    category: $("fact_category").value,
    status: $("fact_status").value,
    source: $("fact_source").value.trim(),
    allow_use: $("fact_allow").checked,
  };
  if (editingFactId) {
    const f = state.facts.find((x) => x.id === editingFactId);
    if (f) Object.assign(f, data);
  } else {
    state.facts.push({ id: "f" + Date.now().toString(36), ...data });
  }
  $("fact_editor").classList.add("hidden");
  editingFactId = null;
  onFactsChanged();
});

// 在生成时记录事实哈希
const _origRenderIdeas = typeof renderIdeas !== "undefined" ? true : false;

function factLabel(id) {
  const f = (state.facts || []).find((x) => x.id === id);
  return f ? f.content.slice(0, 20) : id;
}

