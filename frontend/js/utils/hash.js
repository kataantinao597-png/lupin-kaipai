/* 鲁品开拍 - 内容哈希工具（S7.1）
   用途：冲突检测——比较本地内容哈希与服务器版本，避免静默覆盖。
   纯同步实现（FNV-1a + SHA-256 异步版），无依赖。
*/
(function () {
  "use strict";

  // 稳定序列化：键排序，保证同一内容哈希一致
  function stableStringify(v) {
    if (v === null || v === undefined) return "null";
    if (Array.isArray(v)) return "[" + v.map(stableStringify).join(",") + "]";
    if (typeof v === "object") {
      var keys = Object.keys(v).sort();
      return "{" + keys.map(function (k) {
        return JSON.stringify(k) + ":" + stableStringify(v[k]);
      }).join(",") + "}";
    }
    return JSON.stringify(v);
  }

  // FNV-1a 32 位：同步、快，适合高频脏检查
  function fnv1a(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }

  function quickHash(value) {
    return fnv1a(stableStringify(value));
  }

  // SHA-256（异步，crypto.subtle）：用于与服务端 content_hash 对账
  async function sha256Hex(value) {
    var text = stableStringify(value);
    if (typeof crypto !== "undefined" && crypto.subtle && crypto.subtle.digest) {
      var buf = await crypto.subtle.digest("SHA-256",
        new TextEncoder().encode(text));
      return Array.prototype.map.call(new Uint8Array(buf), function (b) {
        return ("0" + b.toString(16)).slice(-2);
      }).join("");
    }
    // 降级：双 FNV 拼 64 位（非密码学，仅一致性用）
    return fnv1a(text) + fnv1a("~" + text);
  }

  window.HashUtil = {
    stableStringify: stableStringify,
    quickHash: quickHash,
    sha256Hex: sha256Hex,
  };
})();
