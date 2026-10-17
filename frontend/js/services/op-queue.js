/* 鲁品开拍 - 离线写操作队列（S7.3）
 *
 * 网络中断时，写操作存入 IndexedDB 待同步队列（opQueue）；
 * 网络恢复后逐项同步，遇到 409 冲突转人工处理（调 LPConflict）。
 *
 * 用法：
 *   OpQueue.enqueue({ type: "save-work", workId, payload }) -> Promise<id>
 *   OpQueue.pendingCount() -> Promise<number>
 *   OpQueue.drain(syncFn, opts)   // syncFn(op) -> Promise；opts.onConflict(op, err)
 *   OpQueue.isOnline()            // navigator.onLine 封装
 *
 * 与架构组 store 的对接约定：
 * - store 的云端写操作在 fetch 失败（网络错误）时调用 OpQueue.enqueue()，
 *   而不是直接丢弃；恢复在线后 store 调用 OpQueue.drain(syncFn)。
 * - syncFn 由 store 提供：负责把 op.payload 真正写到云端（含 clientVersion）。
 * - onConflict(op, err)：store 决定如何处理 409（一般调 LPConflict.handleSaveConflict）。
 * - 成功同步的 op 自动出队；网络错误则停止 drain 等下次 online 事件。
 */
(function () {
  "use strict";

  var DB_NAME = "lupin_kaipai";
  var STORE = "op_queue";
  var DB_VERSION = 1;
  var MAX_OPS = 500; // 队列上限，防止无限堆积

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!("indexedDB" in window)) {
        reject(new Error("当前浏览器不支持 IndexedDB，离线队列不可用"));
        return;
      }
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          var st = db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
          st.createIndex("by_work", "workId", { unique: false });
          st.createIndex("by_created", "createdAt", { unique: false });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error || new Error("IndexedDB 打开失败")); };
    });
  }

  /** 入队。op: { type, workId, payload, label? } */
  function enqueue(op) {
    op = op || {};
    return openDb()
      .then(drainOldestIfNeeded)
      .then(function (db) {
        return new Promise(function (resolve, reject) {
          var t = db.transaction(STORE, "readwrite");
          var st = t.objectStore(STORE);
          var rec = {
            type: op.type || "unknown",
            workId: op.workId || "",
            label: op.label || op.type || "写操作",
            payload: op.payload || null,
            createdAt: Date.now(),
            attempts: 0,
          };
          var req = st.add(rec);
          req.onsuccess = function () { resolve(req.result); };
          req.onerror = function () { reject(req.error); };
          t.onerror = function () { reject(t.error); };
        });
      });
  }

  function drainOldestIfNeeded(db) {
    return new Promise(function (resolve, reject) {
      var t = db.transaction(STORE, "readwrite");
      var st = t.objectStore(STORE);
      var countReq = st.count();
      countReq.onsuccess = function () {
        if (countReq.result < MAX_OPS) { resolve(db); return; }
        // 删除最旧的 (MAX_OPS 溢出部分 + 1)
        var idx = st.index("by_created");
        var toDelete = countReq.result - MAX_OPS + 1;
        var cur = idx.openCursor();
        var done = 0;
        cur.onsuccess = function () {
          var c = cur.result;
          if (c && done < toDelete) { c.delete(); done++; c.continue(); }
          else resolve(db);
        };
        cur.onerror = function () { reject(cur.error); };
      };
      countReq.onerror = function () { reject(countReq.error); };
    });
  }

  function pendingCount() {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, "readonly");
        var req = t.objectStore(STORE).count();
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error); };
      });
    }).catch(function () { return 0; });
  }

  function listAll() {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, "readonly");
        var idx = t.objectStore(STORE).index("by_created");
        var out = [];
        var cur = idx.openCursor();
        cur.onsuccess = function () {
          var c = cur.result;
          if (c) { out.push(c.value); c.continue(); }
          else resolve(out);
        };
        cur.onerror = function () { reject(cur.error); };
      });
    });
  }

  function remove(id) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(STORE, "readwrite");
        var req = t.objectStore(STORE).delete(id);
        req.onsuccess = function () { resolve(true); };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  function bumpAttempts(id) {
    return openDb().then(function (db) {
      return new Promise(function (resolve) {
        var t = db.transaction(STORE, "readwrite");
        var st = t.objectStore(STORE);
        var req = st.get(id);
        req.onsuccess = function () {
          var rec = req.result;
          if (rec) { rec.attempts = (rec.attempts || 0) + 1; st.put(rec); }
          resolve(true);
        };
        req.onerror = function () { resolve(false); };
      });
    });
  }

  function isNetworkError(err) {
    // fetch 网络层失败：TypeError + message 含 failed/fetch
    return err instanceof TypeError ||
      (err && /failed to fetch|networkerror|load failed|network request failed/i.test(String(err.message || err)));
  }

  /**
   * 逐项同步。
   * syncFn(op) -> Promise<void>，成功 resolve 即出队。
   * opts.onConflict(op, err) -> Promise<"resolved"|"deferred">，409 时调用。
   * 返回 { done, failed, deferred }。
   */
  function drain(syncFn, opts) {
    opts = opts || {};
    var stats = { done: 0, failed: 0, deferred: 0 };
    return listAll().then(function (ops) {
      var chain = Promise.resolve();
      ops.forEach(function (op) {
        chain = chain.then(function () {
          return Promise.resolve()
            .then(function () { return syncFn(op); })
            .then(function () { return remove(op.id).then(function () { stats.done++; }); })
            .catch(function (err) {
              if (window.LPConflict && window.LPConflict.isConflict(err) &&
                  typeof opts.onConflict === "function") {
                // 409 转人工：由调用方弹冲突对话框
                return Promise.resolve(opts.onConflict(op, err)).then(function (r) {
                  if (r === "resolved") { return remove(op.id).then(function () { stats.done++; }); }
                  stats.deferred++;
                });
              }
              if (isNetworkError(err)) {
                // 仍断网：停止 drain，等下次 online
                throw { _stopDrain: true };
              }
              return bumpAttempts(op.id).then(function () { stats.failed++; });
            });
        });
      });
      return chain.then(
        function () { return stats; },
        function (e) { if (e && e._stopDrain) return stats; throw e; }
      );
    });
  }

  function isOnline() {
    return typeof navigator !== "undefined" ? navigator.onLine !== false : true;
  }

  /* 网络恢复时自动尝试同步（store 可覆盖 window.__lupinDrainFn 注入 syncFn） */
  window.addEventListener("online", function () {
    if (window.LPStatus) window.LPStatus.hideOfflineBanner();
    if (typeof window.__lupinDrainFn === "function") {
      drain(window.__lupinDrainFn, { onConflict: window.__lupinConflictFn })
        .then(function (s) {
          if (s.done > 0 && window.LPToast) window.LPToast("已同步 " + s.done + " 个离线操作");
        })
        .catch(function () {});
    }
  });
  window.addEventListener("offline", function () {
    pendingCount().then(function (n) {
      if (window.LPStatus) window.LPStatus.showOfflineBanner(n);
    });
  });

  window.OpQueue = {
    enqueue: enqueue,
    pendingCount: pendingCount,
    listAll: listAll,
    remove: remove,
    drain: drain,
    isOnline: isOnline,
    isNetworkError: isNetworkError,
  };
})();
