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
  // 015: 记录请求归属，响应返回后校验
  const reqWorkId = (typeof works !== "undefined" && works.activeId) || null;
  try {
    state.product = collectForm();
    state.locked = new Set();
    setLoading("正在生成 3 个创意方向…");
    const d = await api("/api/ideas", { product: state.product, facts: usableFacts() }, { signal: workSignal() });
    if (typeof works !== "undefined" && works.activeId !== reqWorkId) return; // 用户已切换作品，丢弃过期响应
    state.ideas = d.ideas;
    state.ideasFactsHash = factsHash();
    renderIdeas();
    checkFactsStale();
    if (typeof scheduleSave === "function") scheduleSave();
    showView("v2");
  } catch (e) { if (isAbortError(e)) return; showErr("err1", e.message); }
  finally { setLoading(null); }
});

