/* 鲁品开拍 - 统一 UI 状态组件（S7.4）
 *
 * 用法：
 *   renderStatus(el, "loading", { text: "正在加载…" })          // 骨架屏/spinner
 *   renderStatus(el, "empty",   { text: "暂无数据", hint: "…" }) // 空数据
 *   renderStatus(el, "error",   { message: "…", onRetry })       // 错误+重试
 *   renderStatus(el, "forbidden", { message })                  // 403 无权限
 *   clearStatus(el)                                             // 恢复
 *   showOfflineBanner() / hideOfflineBanner()                   // 断网横幅（全局）
 *
 * 与架构组的 apiClient/store 对接约定：
 * - 所有渲染函数只操作传入的容器元素，不依赖全局 state。
 * - onRetry 等回调由调用方传入，store 层决定重试逻辑。
 */
(function () {
  "use strict";

  var ICONS = {
    loading: "◌",
    empty: "▢",
    error: "⚠",
    forbidden: "⛔",
    offline: "📡",
  };

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /**
   * 在容器内渲染统一状态。kind: loading | empty | error | forbidden
   * 返回容器元素，方便链式调用。
   */
  function renderStatus(el, kind, opts) {
    if (!el) return el;
    opts = opts || {};
    el.classList.add("lp-status-wrap");
    var icon = ICONS[kind] || ICONS.empty;
    var html = "";
    if (kind === "loading") {
      // skeleton 骨架屏：3 条占位条 + 可选文案
      html =
        '<div class="lp-status lp-loading" role="status" aria-live="polite">' +
        '<div class="lp-skeleton"><span></span><span></span><span class="short"></span></div>' +
        (opts.text ? '<p class="lp-status-text">' + escHtml(opts.text) + "</p>" : "") +
        "</div>";
    } else if (kind === "empty") {
      html =
        '<div class="lp-status lp-empty">' +
        '<div class="lp-status-icon">' + icon + "</div>" +
        '<p class="lp-status-title">' + escHtml(opts.text || "暂无数据") + "</p>" +
        (opts.hint ? '<p class="lp-status-hint">' + escHtml(opts.hint) + "</p>" : "") +
        (opts.actionLabel
          ? '<button class="lp-status-btn" data-lp-status-action="1">' + escHtml(opts.actionLabel) + "</button>"
          : "") +
        "</div>";
    } else if (kind === "error") {
      html =
        '<div class="lp-status lp-error" role="alert">' +
        '<div class="lp-status-icon">' + icon + "</div>" +
        '<p class="lp-status-title">出错了</p>' +
        '<p class="lp-status-hint">' + escHtml(opts.message || "请求失败，请稍后重试") + "</p>" +
        '<button class="lp-status-btn" data-lp-status-action="1">重试</button>' +
        "</div>";
    } else if (kind === "forbidden") {
      html =
        '<div class="lp-status lp-forbidden" role="alert">' +
        '<div class="lp-status-icon">' + icon + "</div>" +
        '<p class="lp-status-title">没有权限</p>' +
        '<p class="lp-status-hint">' + escHtml(opts.message || "当前账号无权查看此内容，请联系管理员") + "</p>" +
        "</div>";
    } else {
      html =
        '<div class="lp-status lp-empty"><p class="lp-status-hint">' +
        escHtml(opts.text || "") + "</p></div>";
    }
    el.innerHTML = html;
    var btn = el.querySelector("[data-lp-status-action]");
    if (btn && typeof opts.onRetry === "function") {
      btn.addEventListener("click", function () { opts.onRetry(); });
    } else if (btn && typeof opts.onAction === "function") {
      btn.addEventListener("click", function () { opts.onAction(); });
    }
    return el;
  }

  function clearStatus(el) {
    if (!el) return;
    el.classList.remove("lp-status-wrap");
    el.innerHTML = "";
  }

  /* ---------- 全局断网横幅 ---------- */
  var bannerEl = null;
  function showOfflineBanner(pendingCount) {
    hideOfflineBanner();
    bannerEl = document.createElement("div");
    bannerEl.id = "lp-offline-banner";
    bannerEl.setAttribute("role", "alert");
    var extra = pendingCount > 0
      ? "（" + pendingCount + " 个操作待同步）"
      : "";
    bannerEl.innerHTML =
      '<span class="lp-offline-dot"></span>网络已断开，写操作将暂存本地，恢复后自动同步' +
      escHtml(extra);
    document.body.appendChild(bannerEl);
  }
  function hideOfflineBanner() {
    if (bannerEl && bannerEl.parentNode) bannerEl.parentNode.removeChild(bannerEl);
    bannerEl = null;
  }
  function isOfflineBannerShown() { return !!bannerEl; }

  /* ---------- 导出（挂 window，供原生 script 标签使用） ---------- */
  window.LPStatus = {
    render: renderStatus,
    clear: clearStatus,
    showOfflineBanner: showOfflineBanner,
    hideOfflineBanner: hideOfflineBanner,
    isOfflineBannerShown: isOfflineBannerShown,
  };
})();
