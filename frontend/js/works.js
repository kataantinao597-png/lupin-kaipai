/* ============================================================
   V4: 统一作品数据层
   ============================================================ */
const WORKS_KEY = "lupin_works_v4";
const WORKS_VERSION = 1;
let works = { version: WORKS_VERSION, activeId: null, items: [] };
let saveTimer = null;
let saveStatus = "saved"; // saved | saving | failed | unsaved

function genId() {
  return "w_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8);
}

function storyboardHash(sb) {
  if (!sb || !sb.shots) return "";
  const parts = [sb.product_summary || ""];
  sb.shots.forEach((s) => parts.push([s.shot_number, s.duration_seconds, s.scene_description, s.shooting_action, s.voiceover].join("|")));
  let h = 0;
  const str = parts.join("##");
  for (let i = 0; i < str.length; i++) { h = ((h << 5) - h + str.charCodeAt(i)) | 0; }
  return (h >>> 0).toString(36);
}

function assignShotUids(sb) {
  // 客户端稳定 ID，不发送给后端
  (sb.shots || []).forEach((s) => {
    if (!s._uid) s._uid = "s_" + Math.random().toString(36).slice(2, 10);
  });
}

/* 007: 永久镜头 ID。后端忽略多余字段，shot_id 由前端维护，
   重排/删除/重生成后仍能关联拍摄记录、审核结果。旧数据没有就补。 */
function genShotId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    try { return "shot_" + crypto.randomUUID().replace(/-/g, "").slice(0, 12); } catch (e) {}
  }
  return "shot_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
}

function ensureShotIds(sb) {
  (sb && sb.shots || []).forEach((s) => {
    if (!s.shot_id) s.shot_id = genShotId();
  });
  return sb;
}

function stripUids(obj) {
  // 深拷贝并移除 _uid，用于 API 请求
  if (!obj) return obj;
  const copy = JSON.parse(JSON.stringify(obj));
  (copy.shots || []).forEach((s) => delete s._uid);
  return copy;
}

function collectWorkData() {
  return {
    product: state.product,
    facts: state.facts || [],
    factsUpdatedAt: state.factsUpdatedAt || 0,
    ideas: state.ideas,
    ideasFactsHash: state.ideasFactsHash || "",
    direction: state.direction,
    storyboard: stripUids(state.storyboard),
    sbFactsHash: state.sbFactsHash || "",
    locked: [...state.locked],
    feasibility: state.feasibility ? {
      results: state.feasibility,
      // 009: 用检查时刻记录的 hash，不在保存时重算（重算会掩盖"分镜已变、报告过期"）
      storyboardHash: state.feasibilityHash || storyboardHash(state.storyboard),
      checkedAt: Date.now(),
    } : null,
    risks: state.risks,
    shoot: state.shoot,
    stage: currentStage(),
  };
}

function applyWorkData(data) {
  state.product = data.product || null;
  state.facts = data.facts || [];
  state.factsUpdatedAt = data.factsUpdatedAt || 0;
  state.ideasFactsHash = (data.ideas && data.ideasFactsHash) || "";
  state.sbFactsHash = (data.storyboard && data.sbFactsHash) || "";
  state.ideas = data.ideas || [];
  state.direction = data.direction || "";
  state.storyboard = data.storyboard || null;
  if (state.storyboard) { assignShotUids(state.storyboard); ensureShotIds(state.storyboard); }
  state.locked = new Set(data.locked || []);
  state.feasibility = (data.feasibility && data.feasibility.results) || null;
  state.feasibilityHash = (data.feasibility && data.feasibility.storyboardHash) || "";
  state.risks = data.risks || [];
  state.shoot = data.shoot || { key: "", shots: {} };
  state.shootFilter = "all";
  // 恢复表单
  if (state.product) fillForm(state.product);
  renderAll();
  if (typeof renderFacts === "function") renderFacts();
  if (typeof checkFactsStale === "function") setTimeout(checkFactsStale, 500);
}

function currentStage() {
  if (state.storyboard && state.storyboard.shots && state.storyboard.shots.length) {
    const st = shootStats();
    if (st.done > 0 || st.reshoot > 0) return "v5";
    return "v3";
  }
  if (state.ideas && state.ideas.length) return "v2";
  if (state.product) return "v1";
  return "v0";
}

function setSaveStatus(s) {
  saveStatus = s;
  const el = $("save_status");
  if (!el) return;
  // S7.3：localStorage 只做本地缓存，明确标注（云端以数据库为准）
  const labels = { saved: "✓ 已保存（本地缓存）", saving: "⏳ 保存中…", failed: "⚠ 保存失败", unsaved: "● 未保存" };
  el.textContent = labels[s] || s;
  el.className = "save-status " + s + " cache-note";
  el.title = "本地缓存，可能过期；多设备/云端以服务器数据库为准";
}

function persistWorks() {
  setSaveStatus("saving");
  try {
    localStorage.setItem(WORKS_KEY, JSON.stringify(works));
    setSaveStatus("saved");
    return true;
  } catch (e) {
    setSaveStatus("failed");
    return false;
  }
}

function scheduleSave() {
  setSaveStatus("unsaved");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveActiveWork(), 1500);
}

// 显式标记数据已变更（用于用户操作后），替代渲染时的隐式保存
function markDirty() {
  if (state.storyboard || state.product) scheduleSave();
}

function saveActiveWork() {
  const w = works.items.find((x) => x.id === works.activeId);
  if (!w) return false;
  w.data = collectWorkData();
  w.updatedAt = Date.now();
  if (!w.name && state.product) w.name = state.product.product_name || "未命名作品";
  return persistWorks();
}

function createWork(name) {
  // 先保存当前
  saveActiveWork();
  const w = {
    id: genId(),
    name: name || ("作品 " + (works.items.length + 1)),
    createdAt: Date.now(),
    updatedAt: Date.now(),
    data: { product: null, facts: [], factsUpdatedAt: 0, ideas: [], direction: "", storyboard: null, locked: [], feasibility: null, risks: [], shoot: { key: "", shots: {} }, stage: "v0" },
  };
  works.items.push(w);
  works.activeId = w.id;
  applyWorkData(w.data);
  persistWorks();
  return w;
}

function switchWork(id) {
  if (id === works.activeId) return true;
  if (!saveActiveWork()) {
    if (!confirm("当前作品保存失败，切换将丢失未保存的修改。仍要切换吗？")) return false;
  }
  // S7.4：取消旧作品未完成的请求，防止旧数据污染新作品
  if (window.WorkGuard) {
    window.WorkGuard.abortAll();
    window.WorkGuard.setActiveWork(id);
  }
  const w = works.items.find((x) => x.id === id);
  if (!w) return false;
  works.activeId = id;
  applyWorkData(w.data);
  persistWorks();
  return true;
}

function deleteWork(id) {
  const w = works.items.find((x) => x.id === id);
  if (!w) return;
  if (!confirm(`确定删除作品「${w.name}」吗？此操作不可恢复。`)) return;
  works.items = works.items.filter((x) => x.id !== id);
  if (works.activeId === id) {
    works.activeId = works.items.length ? works.items[0].id : null;
    if (works.activeId) applyWorkData(works.items[0].data);
    else { // 无作品时重置为空状态
      applyWorkData({ product: null, ideas: [], direction: "", storyboard: null, locked: [], feasibility: null, risks: [], shoot: { key: "", shots: {} }, stage: "v0" });
    }
  }
  persistWorks();
  renderWorks();
}

function renameWork(id, name) {
  const w = works.items.find((x) => x.id === id);
  if (w && name.trim()) { w.name = name.trim(); w.updatedAt = Date.now(); persistWorks(); renderWorks(); }
}

function loadWorks() {
  try {
    const raw = localStorage.getItem(WORKS_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      if (data.version === WORKS_VERSION && Array.isArray(data.items)) {
        works = data;
        return;
      }
    }
  } catch (e) { /* 损坏时走迁移/新建 */ }
  works = { version: WORKS_VERSION, activeId: null, items: [] };
}

function migrateLegacyShoot() {
  // 迁移旧版 lupin_shoot_v1_* 数据
  let migrated = 0;
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith("lupin_shoot_v1_") && !k.includes("_migrated")) {
        try {
          const data = JSON.parse(localStorage.getItem(k));
          // 旧数据无法可靠关联到作品，记录但不强行迁移
          console.log("[V4迁移] 发现旧版拍摄状态:", k, "镜头数:", Object.keys(data.shots || {}).length);
        } catch (e) {}
      }
    }
  } catch (e) {}
  return migrated;
}

function isFeasibilityStale() {
  if (!state.feasibility) return false;
  return storyboardHash(state.storyboard) !== state.feasibilityHash;
}

