/* 鲁品开拍 - 作品存储状态机（S7.3）
   状态：clean → dirty → saving → saved | error
   - markDirty()：本地有未保存修改
   - save(saver)：saver 为 async 函数，负责实际落库，成功需返回 { version }（服务器版本号）；
     成功后状态 saved 并更新 serverVersion；失败状态 error，dirty 数据保留，绝不伪装已保存
   - checkConflict(serverVersion)：服务器版本比本地新且本地有 dirty → 冲突，不静默覆盖
   用法：
     WorkStore.load(workId, data, serverVersion);
     WorkStore.markDirty();
     await WorkStore.save(async () => {
       const r = await ApiClient.patch("/api/v1/works/" + id, { data, version });
       return { version: r.data.version };
     });
*/
(function () {
  "use strict";

  var STATUS = { CLEAN: "clean", DIRTY: "dirty", SAVING: "saving", SAVED: "saved", ERROR: "error" };

  var store = {
    workId: null,
    data: null,          // 本地草稿（dirty 数据失败时保留在此）
    serverVersion: 0,    // 上次成功保存时的服务器版本
    serverHash: "",      // 上次成功保存时的内容哈希（供冲突检测）
    status: STATUS.CLEAN,
    lastError: null,
    lastSavedAt: 0,
    listeners: [],
  };

  function setStatus(s, err) {
    store.status = s;
    store.lastError = err || null;
    store.listeners.forEach(function (fn) {
      try { fn(s, store); } catch (e) { /* 忽略监听器异常 */ }
    });
  }

  function onChange(fn) {
    if (typeof fn === "function") store.listeners.push(fn);
    return function () {
      var i = store.listeners.indexOf(fn);
      if (i >= 0) store.listeners.splice(i, 1);
    };
  }

  // 载入作品：重置为 clean
  function load(workId, data, serverVersion) {
    store.workId = workId;
    store.data = data;
    store.serverVersion = serverVersion || 0;
    store.serverHash = (typeof HashUtil === "object") ? HashUtil.quickHash(data) : "";
    setStatus(STATUS.CLEAN, null);
  }

  function markDirty() {
    if (store.status === STATUS.SAVING) return; // 保存中不改状态
    if (store.status !== STATUS.DIRTY) setStatus(STATUS.DIRTY, null);
  }

  function isDirty() {
    return store.status === STATUS.DIRTY || store.status === STATUS.ERROR;
  }

  // saver: async () => ({ version })；抛错即保存失败
  async function save(saver) {
    if (store.status === STATUS.SAVING) return { ok: false, reason: "saving" };
    if (typeof saver !== "function") throw new Error("WorkStore.save 需要传入 saver 函数");
    setStatus(STATUS.SAVING, null);
    try {
      var ret = await saver();
      var v = ret && ret.version != null ? ret.version : store.serverVersion;
      store.serverVersion = v;
      store.serverHash = (typeof HashUtil === "object") ? HashUtil.quickHash(store.data) : "";
      store.lastSavedAt = Date.now();
      setStatus(STATUS.SAVED, null);
      return { ok: true, version: v };
    } catch (e) {
      // 失败：状态 error，dirty 数据保留在 store.data，不伪装已保存
      setStatus(STATUS.ERROR, e);
      return { ok: false, error: e };
    }
  }

  // 服务器版本比本地新 → 有人先改了；本地若还有 dirty 则冲突
  function checkConflict(serverVersion) {
    var sv = Number(serverVersion) || 0;
    if (sv > store.serverVersion && isDirty()) {
      return { conflict: true, serverVersion: sv, localVersion: store.serverVersion };
    }
    if (sv > store.serverVersion) {
      // 本地干净：直接跟进服务器版本
      store.serverVersion = sv;
    }
    return { conflict: false };
  }

  function getStatus() { return store.status; }
  function getState() {
    return {
      workId: store.workId, status: store.status,
      serverVersion: store.serverVersion, lastError: store.lastError,
      lastSavedAt: store.lastSavedAt, dirty: isDirty(),
    };
  }

  window.WorkStore = {
    STATUS: STATUS,
    load: load,
    markDirty: markDirty,
    isDirty: isDirty,
    save: save,
    checkConflict: checkConflict,
    getStatus: getStatus,
    getState: getState,
    onChange: onChange,
    // 供调试/渐进接入：读写本地草稿引用
    _store: store,
  };
})();
