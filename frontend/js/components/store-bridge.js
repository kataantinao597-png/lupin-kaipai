/* 鲁品开拍 - WorkStore / ApiClient / 冲突对话框桥接（S7.3）
 *
 * 把架构组的 WorkStore 状态机与本组的 UI 组件接起来：
 * 1. 订阅 WorkStore.onChange，把云端保存状态反映到 #save_status
 *    （dirty/saving/saved/error + 服务器版本号；失败绝不伪装已保存）。
 * 2. saveWithConflictHandling(workId, getData)：WorkStore.save 的封装；
 *    409 冲突时弹 LPConflict 对话框：
 *      - 重新加载：GET 云端版本 → WorkStore.load → 回调 onReloaded
 *      - 保留覆盖：二次确认后，用云端最新 version 强制 PATCH
 *      - 查看差异：拉取云端数据，与本地做字段级对比
 * 3. 离线：网络错误时写操作进 OpQueue，恢复后 drain（调 window.__lupinDrainFn）。
 *
 * 对接约定（给架构组/后续 store 逻辑）：
 * - 云端保存入口统一走 StoreBridge.saveCloud(workId, getData, opts)。
 * - getData() 返回要保存的 data 对象；opts.onReloaded(serverData) 可选。
 */
(function () {
  "use strict";

  var SAVE_STATUS_EL = "save_status";

  function $(id) { return document.getElementById(id); }

  /* ---------- 1. 保存状态订阅 → UI ---------- */
  var LABELS = {
    clean: "✓ 已同步",
    dirty: "● 有未保存修改",
    saving: "⏳ 正在保存到云端…",
    saved: "✓ 已保存到云端",
    error: "⚠ 云端保存失败",
  };

  function renderSaveStatus(status, st) {
    var el = $(SAVE_STATUS_EL);
    if (!el) return;
    var label = LABELS[status] || status;
    if (status === "saved" && st && st.serverVersion) {
      label += "（v" + st.serverVersion + "）";
    }
    if (status === "error" && st && st.lastError) {
      label += "：" + String(st.lastError.message || st.lastError).slice(0, 40);
    }
    el.textContent = label;
    el.className = "save-status cloud-" + status;
    el.title = status === "saved"
      ? "云端已同步（服务器版本 v" + (st ? st.serverVersion : "?") + "）"
      : "云端保存状态；本地 localStorage 仅为缓存，可能过期";
  }

  function initSaveStatusBridge() {
    if (typeof WorkStore !== "object" || !WorkStore.onChange) return false;
    WorkStore.onChange(function (status, store) {
      renderSaveStatus(status, store);
    });
    return true;
  }

  /* ---------- 2. 带冲突处理的云端保存 ---------- */

  function parseServerVersionFromConflict(err) {
    // 后端 409 message: "作品已被他人修改（当前版本 {v}），请刷新后重试"
    var m = /当前版本\s*(\d+)/.exec(String((err && err.message) || ""));
    return m ? parseInt(m[1], 10) : null;
  }

  function fetchServerWork(workId) {
    // ApiClient.get 返回 { data }，v1 信封经 response 解析后 data.data 为业务体
    return ApiClient.get("/api/v1/works/" + encodeURIComponent(workId))
      .then(function (r) {
        var body = r.data && r.data.data !== undefined ? r.data.data : r.data;
        return body;
      });
  }

  /**
   * workId: 云端作品 id；getData(): 返回本地 data；
   * opts: { onReloaded(serverData), workName }
   */
  function saveCloud(workId, getData, opts) {
    opts = opts || {};
    if (typeof WorkStore !== "object" || typeof ApiClient !== "object") {
      return Promise.reject(new Error("WorkStore/ApiClient 未加载"));
    }
    var localVersion = WorkStore.getState().serverVersion;

    function doPatch(version) {
      var data = getData();
      return ApiClient.patch("/api/v1/works/" + encodeURIComponent(workId), {
        data: data, version: version,
      }).then(function (r) {
        var body = r.data && r.data.data !== undefined ? r.data.data : r.data;
        return { version: body.version != null ? body.version : version };
      });
    }

    // 离线：直接进队列，不走 WorkStore.save（避免 error 状态误导）
    if (typeof OpQueue === "object" && !OpQueue.isOnline()) {
      return OpQueue.enqueue({
        type: "save-work", workId: workId,
        label: "保存作品「" + (opts.workName || workId) + "」",
        payload: { data: getData(), baseVersion: localVersion },
      }).then(function () {
        if (window.LPStatus) window.LPStatus.showOfflineBanner(1);
        return { ok: true, queued: true };
      });
    }

    return WorkStore.save(function () { return doPatch(localVersion); })
      .then(function (ret) {
        if (ret.ok) return ret;
        var err = ret.error;
        if (window.LPConflict && window.LPConflict.isConflict(err)) {
          var serverVersion = parseServerVersionFromConflict(err);
          return window.LPConflict.handleSaveConflict(err, {
            workName: opts.workName || workId,
            serverVersion: serverVersion,
            localVersion: localVersion,
            getLocalData: getData,
            getServerData: function () {
              // 同步回调里不能 await：返回占位，真正拉取在 onReload/onForce 里做
              return window.__lupinLastServerData || {};
            },
            onReload: function () {
              return fetchServerWork(workId).then(function (serverBody) {
                window.__lupinLastServerData = serverBody;
                WorkStore.load(workId, serverBody.data || serverBody,
                  serverBody.version != null ? serverBody.version : serverVersion);
                if (typeof opts.onReloaded === "function") opts.onReloaded(serverBody);
              });
            },
            onForce: function () {
              // 强制覆盖：先取最新 version，再 PATCH（二次确认已在对话框内完成）
              return fetchServerWork(workId).then(function (serverBody) {
                var v = serverBody.version != null ? serverBody.version : serverVersion;
                return doPatch(v).then(function (r2) {
                  WorkStore.load(workId, getData(), r2.version);
                });
              });
            },
          });
        }
        throw err;
      });
  }

  /* ---------- 3. 离线恢复自动同步 ---------- */
  // OpQueue 的 online 钩子调这个（drain 每个 op 时用 saveCloud 重放）
  window.__lupinDrainFn = function (op) {
    if (op.type === "save-work" && op.payload) {
      var data = op.payload.data;
      var baseVersion = op.payload.baseVersion;
      return ApiClient.patch("/api/v1/works/" + encodeURIComponent(op.workId), {
        data: data, version: baseVersion,
      }).then(function () {});
    }
    return Promise.reject(new Error("未知离线操作类型: " + op.type));
  };
  // drain 遇到 409 时转人工：弹冲突对话框（deferred 则保留在队列）
  window.__lupinConflictFn = function (op, err) {
    if (!window.LPConflict) return Promise.resolve("deferred");
    return window.LPConflict.handleSaveConflict(err, {
      workName: op.label || op.workId,
      serverVersion: parseServerVersionFromConflict(err),
      localVersion: op.payload && op.payload.baseVersion,
      getLocalData: function () { return (op.payload && op.payload.data) || {}; },
      getServerData: function () { return window.__lupinLastServerData || {}; },
      onReload: function () {
        return fetchServerWork(op.workId).then(function (serverBody) {
          window.__lupinLastServerData = serverBody;
          return "resolved"; // 视为已处理（用户选择了云端版本）
        });
      },
      onForce: function () {
        return fetchServerWork(op.workId).then(function (serverBody) {
          var v = serverBody.version;
          return ApiClient.patch("/api/v1/works/" + encodeURIComponent(op.workId), {
            data: op.payload.data, version: v,
          }).then(function () { return "resolved"; });
        });
      },
    }).then(function (r) {
      return r === "deferred" ? "deferred" : "resolved";
    }).catch(function () { return "deferred"; });
  };

  // 页面加载时如果已有 WorkStore，立即订阅；否则延迟重试一次
  if (!initSaveStatusBridge()) {
    setTimeout(initSaveStatusBridge, 500);
  }

  window.StoreBridge = {
    saveCloud: saveCloud,
    fetchServerWork: fetchServerWork,
    parseServerVersionFromConflict: parseServerVersionFromConflict,
    initSaveStatusBridge: initSaveStatusBridge,
  };
})();
