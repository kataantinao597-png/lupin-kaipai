/* 鲁品开拍 - 过期审核标记（S7.4）
 *
 * 规则：审核结果过期（status === "expired" 或 fresh === false）时，
 * 必须用明显样式标记"已过期"，绝不允许展示为"通过"状态。
 *
 * 用法：
 *   LPExpiry.isExpired(review)          // review 对象 -> bool
 *   LPExpiry.badge(review)              // -> 徽章 HTML 字符串（未过期返回 ""）
 *   LPExpiry.decorateCard(cardEl, review)// 给审核卡片加过期样式+徽章
 *   LPExpiry.notice(review)             // -> 提示条 HTML（含 stale_reason）
 *
 * 后端字段约定（/api/v1/{work_id}/reviews/latest）：
 *   { status, fresh, stale_reason, content_hash, version, ... }
 */
(function () {
  "use strict";

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /** 审核是否已过期：显式 expired，或新鲜度校验失败 */
  function isExpired(review) {
    if (!review) return false;
    if (review.status === "expired") return true;
    if (review.fresh === false) return true;
    return false;
  }

  /** 是否"需要重新审核"（changes_requested / needs_changes / blocked 也视为需处理） */
  function needsRework(review) {
    if (!review) return false;
    return review.status === "changes_requested" ||
      review.status === "needs_changes" ||
      review.status === "blocked";
  }

  /** 过期徽章 HTML；未过期返回空字符串 */
  function badge(review) {
    if (!isExpired(review)) return "";
    return '<span class="lp-badge lp-badge-expired" title="' +
      escHtml(review.stale_reason || "内容已变更，该审核结论已过期") +
      '">已过期</span>';
  }

  /** 需返工徽章 */
  function reworkBadge(review) {
    if (!needsRework(review)) return "";
    return '<span class="lp-badge lp-badge-rework">需重新审核</span>';
  }

  /** 顶部提示条（含过期原因），未过期返回空字符串 */
  function notice(review) {
    if (!isExpired(review)) return "";
    var reason = review.stale_reason || "内容已变更，该审核结论已自动过期，请重新审核";
    return '<div class="lp-expiry-notice" role="alert">' +
      '<strong>⚠ 审核已过期：</strong>' + escHtml(reason) + "</div>";
  }

  /**
   * 装饰审核结果卡片：加过期样式类 + 在标题行前插入徽章。
   * cardEl: 审核结果容器；titleSel: 标题元素选择器（可选）。
   */
  function decorateCard(cardEl, review, titleSel) {
    if (!cardEl || !isExpired(review)) return;
    cardEl.classList.add("lp-review-expired");
    var b = document.createElement("span");
    b.innerHTML = badge(review);
    var anchor = titleSel ? cardEl.querySelector(titleSel) : cardEl.firstChild;
    if (anchor) cardEl.insertBefore(b, anchor);
    else cardEl.appendChild(b);
    // 插入原因提示条到卡片顶部
    var n = document.createElement("div");
    n.innerHTML = notice(review);
    cardEl.insertBefore(n.firstChild, cardEl.firstChild);
  }

  window.LPExpiry = {
    isExpired: isExpired,
    needsRework: needsRework,
    badge: badge,
    reworkBadge: reworkBadge,
    notice: notice,
    decorateCard: decorateCard,
  };
})();
