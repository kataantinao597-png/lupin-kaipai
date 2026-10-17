/* 鲁品开拍 - 版本冲突对话框（S7.3）
 *
 * 场景：云端保存时带 clientVersion，后端返回 409（业务码 4090）时调用。
 * 绝不静默覆盖：弹出对话框，提供 [重新加载] [查看差异] [保留我的覆盖]；
 * "保留我的覆盖" 需要二次确认（明确告知会覆盖他人修改）。
 *
 * 用法：
 *   LPConflict.isConflict(err)                      // err.status===409 或 err.code===4090
 *   LPConflict.show({ workName, serverVersion, localVersion,
 *                     getLocalData, getServerData })   // -> Promise<"reload"|"force"|null>
 *   LPConflict.handleSaveConflict(err, handlers)     // 一站式：检测→弹窗→分发
 *     handlers: { onReload(), onForce(), getLocalData(), getServerData() }
 *
 * 与架构组 store 的对接约定：
 * - store 的云端 save 捕获 409 后调用 LPConflict.handleSaveConflict。
 * - onReload：store 重新拉取云端版本并应用；onForce：store 用 version=serverVersion 强制覆盖保存。
 * - getLocalData/getServerData 返回可 JSON 序列化的对象，用于"查看差异"。
 */
(function () {
  "use strict";

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /** 是否为版本冲突错误（HTTP 409 / 业务码 4090） */
  function isConflict(err) {
    if (!err) return false;
    return err.status === 409 || err.code === 4090 ||
      (typeof err.message === "string" && /版本冲突|version conflict/i.test(err.message));
  }

  /* ---------- 简易 JSON 差异渲染 ---------- */
  // 扁平化对象为 path -> 值，便于对比
  function flatten(obj, prefix, out) {
    out = out || {};
    prefix = prefix || "";
    if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) {
      var keys = Object.keys(obj);
      if (!keys.length) { out[prefix || "(空对象)"] = "{}"; return out; }
      keys.forEach(function (k) {
        // 跳过内部 uid 之类噪音字段
        if (/_uid$/i.test(k)) return;
        flatten(obj[k], prefix ? prefix + "." + k : k, out);
      });
    } else if (Array.isArray(obj)) {
      out[prefix || "(root)"] = "[数组，长度 " + obj.length + "]";
    } else {
      var v = String(obj);
      out[prefix || "(root)"] = v.length > 80 ? v.slice(0, 80) + "…" : v;
    }
    return out;
  }

  function renderDiff(localData, serverData) {
    var l = flatten(localData), s = flatten(serverData);
    var keys = {};
    Object.keys(l).forEach(function (k) { keys[k] = 1; });
    Object.keys(s).forEach(function (k) { keys[k] = 1; });
    var rows = [];
    Object.keys(keys).sort().forEach(function (k) {
      var inL = Object.prototype.hasOwnProperty.call(l, k);
      var inS = Object.prototype.hasOwnProperty.call(s, k);
      if (inL && inS && l[k] === s[k]) return; // 相同跳过
      var cls = !inL ? "diff-add" : !inS ? "diff-del" : "diff-mod";
      rows.push(
        '<tr class="' + cls + '"><td class="diff-path">' + escHtml(k) + "</td>" +
        "<td>" + (inL ? escHtml(l[k]) : '<span class="diff-na">—</span>') + "</td>" +
        "<td>" + (inS ? escHtml(s[k]) : '<span class="diff-na">—</span>') + "</td></tr>"
      );
    });
    if (!rows.length) {
      return '<p class="lp-diff-empty">两者内容一致（可能仅版本号不同）。</p>';
    }
    return (
      '<table class="lp-diff-table"><thead><tr><th>字段</th><th>我的版本</th><th>云端版本</th></tr></thead>' +
      "<tbody>" + rows.join("") + "</tbody></table>"
    );
  }

  /* ---------- 对话框 DOM ---------- */
  var overlay = null;

  function closeDialog() {
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    overlay = null;
  }

  /**
   * 显示冲突对话框。resolve("reload" | "force" | null)。
   * opts: { workName, serverVersion, localVersion, getLocalData, getServerData }
   */
  function show(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      closeDialog();
      overlay = document.createElement("div");
      overlay.className = "lp-modal-overlay";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.innerHTML =
        '<div class="lp-modal">' +
        '<h3 class="lp-modal-title">⚠ 云端有新版本</h3>' +
        '<p class="lp-modal-desc">作品「' + escHtml(opts.workName || "未命名作品") +
        "」在云端已被更新（云端版本 v" + escHtml(opts.serverVersion) +
        "，本地 v" + escHtml(opts.localVersion) +
        "）。为避免覆盖他人修改，请选择处理方式：</p>" +
        '<div class="lp-diff-wrap hidden"></div>' +
        '<div class="lp-modal-actions">' +
        '<button class="lp-btn lp-btn-primary" data-act="reload">重新加载云端版本</button>' +
        '<button class="lp-btn" data-act="diff">查看差异</button>' +
        '<button class="lp-btn lp-btn-danger" data-act="force">保留我的，覆盖云端</button>' +
        '<button class="lp-btn lp-btn-ghost" data-act="cancel">稍后处理</button>' +
        "</div></div>";

      var diffWrap = overlay.querySelector(".lp-diff-wrap");
      var diffShown = false;

      overlay.addEventListener("click", function (e) {
        var act = e.target.getAttribute && e.target.getAttribute("data-act");
        if (!act) return;
        if (act === "cancel") { closeDialog(); resolve(null); }
        else if (act === "reload") { closeDialog(); resolve("reload"); }
        else if (act === "diff") {
          if (!diffShown) {
            diffShown = true;
            var local = {}, server = {};
            try { local = (opts.getLocalData || function () { return {}; })(); } catch (e2) {}
            try { server = (opts.getServerData || function () { return {}; })(); } catch (e3) {}
            diffWrap.innerHTML = renderDiff(local, server);
            diffWrap.classList.remove("hidden");
            e.target.textContent = "收起差异";
          } else {
            diffShown = false;
            diffWrap.classList.add("hidden");
            e.target.textContent = "查看差异";
          }
        }
        else if (act === "force") {
          // 二次确认：明确告知会覆盖他人修改
          showForceConfirm(function (ok) {
            if (ok) { closeDialog(); resolve("force"); }
            // 取消则回到冲突对话框（不关闭）
          });
        }
      });

      document.body.appendChild(overlay);
    });
  }

  /** "保留我的覆盖"的二次确认，resolve(true/false) */
  function showForceConfirm(cb) {
    var ov = document.createElement("div");
    ov.className = "lp-modal-overlay lp-modal-nested";
    ov.setAttribute("role", "alertdialog");
    ov.setAttribute("aria-modal", "true");
    ov.innerHTML =
      '<div class="lp-modal lp-modal-sm">' +
      '<h3 class="lp-modal-title">确定要用本地版本覆盖云端吗？</h3>' +
      '<p class="lp-modal-desc"><strong>这会覆盖他人在云端的修改，且不可撤销。</strong>' +
      "建议先「查看差异」确认，或联系对方同步后再覆盖。</p>" +
      '<div class="lp-modal-actions">' +
      '<button class="lp-btn lp-btn-danger" data-act="yes">确定覆盖</button>' +
      '<button class="lp-btn" data-act="no">取消</button>' +
      "</div></div>";
    ov.addEventListener("click", function (e) {
      var act = e.target.getAttribute && e.target.getAttribute("data-act");
      if (act === "yes") { ov.parentNode.removeChild(ov); cb(true); }
      else if (act === "no") { ov.parentNode.removeChild(ov); cb(false); }
    });
    document.body.appendChild(ov);
  }

  /**
   * 一站式冲突处理：检测→弹窗→按选择分发。
   * handlers: { workName, serverVersion, localVersion,
   *             getLocalData(), getServerData(),
   *             onReload() -> Promise, onForce() -> Promise }
   * 返回 Promise，resolve 为处理结果字符串。
   */
  function handleSaveConflict(err, handlers) {
    handlers = handlers || {};
    if (!isConflict(err)) return Promise.reject(err);
    return show({
      workName: handlers.workName,
      serverVersion: handlers.serverVersion != null ? String(handlers.serverVersion) : "?",
      localVersion: handlers.localVersion != null ? String(handlers.localVersion) : "?",
      getLocalData: handlers.getLocalData,
      getServerData: handlers.getServerData,
    }).then(function (choice) {
      if (choice === "reload" && typeof handlers.onReload === "function") {
        return handlers.onReload().then(function () { return "reloaded"; });
      }
      if (choice === "force" && typeof handlers.onForce === "function") {
        return handlers.onForce().then(function () { return "forced"; });
      }
      return "deferred"; // 用户选了稍后处理
    });
  }

  window.LPConflict = {
    isConflict: isConflict,
    show: show,
    showForceConfirm: showForceConfirm,
    handleSaveConflict: handleSaveConflict,
    // 导出供自检
    _renderDiff: renderDiff,
    _flatten: flatten,
  };
})();
