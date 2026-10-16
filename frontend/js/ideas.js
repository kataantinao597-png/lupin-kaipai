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
      ${it.fact_refs && it.fact_refs.length ? `<div class="row"><span class="k">事实依据</span><span>${esc(it.fact_refs.map((id) => factLabel(id)).join("、"))}</span></div>` : `<div class="row"><span class="k">事实依据</span><span class="muted">无直接引用，请核实描述</span></div>`}
      <div class="actions"><button class="primary" onclick="selectIdea(${i})">选择这个方向并生成分镜 →</button></div>
    </div>`).join("");
}
async function selectIdea(i) {
  showErr("err2", "");
  const it = state.ideas[i];
  state.direction = it.name + "：" + it.tagline;
  try {
    setLoading("正在生成结构化分镜…（约半分钟）");
    const d = await api("/api/storyboard", { product: state.product, direction: state.direction, facts: usableFacts() });
    state.storyboard = d.storyboard;
    if (typeof assignShotUids === 'function') assignShotUids(state.storyboard);
    state.risks = [];
    renderShots();
    await refreshValidate();
    showView("v3");
  } catch (e) { showErr("err2", e.message); }
  finally { setLoading(null); }
}
$("btn_back1").addEventListener("click", () => showView("v1"));
$("btn_regen_ideas").addEventListener("click", () => { showView("v1"); $("btn_ideas").click(); });

