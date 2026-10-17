/* 鲁品开拍 - 作品切换请求隔离（S7.4）
 *
 * 切换作品时取消未完成的旧作品请求，防止旧数据污染新作品。
 *
 * 用法：
 *   WorkGuard.track(controller, workId)   // 登记一个进行中的请求
 *   WorkGuard.abortAll()                  // 取消全部登记的请求（切换作品时调用）
 *   WorkGuard.signal()                    // 取当前 AbortSignal，用于 fetch
 *   WorkGuard.setActiveWork(id)           // 标记当前作品；旧作品的请求自动失效
 *   WorkGuard.isCurrent(workId)           // 响应回来时校验归属
 *
 * 与架构组 store 的对接约定：
 * - store 在 switchWork 开头调用 WorkGuard.abortAll() + WorkGuard.setActiveWork(id)。
 * - 所有作品相关 fetch 传入 { signal: WorkGuard.signal() }，并在 then 回调里
 *   用 WorkGuard.isCurrent(workId) 校验归属（双保险，对应 015 的 work_id 归属校验）。
 */
(function () {
  "use strict";

  var controllers = new Set();
  var activeWorkId = null;
  var currentController = null;

  function track(controller, workId) {
    if (!controller) return controller;
    controller.__lupinWorkId = workId || activeWorkId;
    controllers.add(controller);
    // 请求结束自动注销
    if (controller.signal) {
      controller.signal.addEventListener("abort", function () {
        controllers.delete(controller);
      }, { once: true });
    }
    return controller;
  }

  function untrack(controller) {
    controllers.delete(controller);
  }

  function abortAll() {
    controllers.forEach(function (c) {
      try { c.abort(); } catch (e) {}
    });
    controllers.clear();
    // 换一个新的 signal，让之后发起的请求使用
    currentController = new AbortController();
    return currentController.signal;
  }

  function signal() {
    if (!currentController || currentController.signal.aborted) {
      currentController = new AbortController();
    }
    return currentController.signal;
  }

  function setActiveWork(id) {
    activeWorkId = id;
  }

  function getActiveWork() {
    return activeWorkId;
  }

  /** 响应归属校验：只接受当前作品的响应 */
  function isCurrent(workId) {
    if (workId == null || activeWorkId == null) return true; // 无上下文不拦截
    return String(workId) === String(activeWorkId);
  }

  window.WorkGuard = {
    track: track,
    untrack: untrack,
    abortAll: abortAll,
    signal: signal,
    setActiveWork: setActiveWork,
    getActiveWork: getActiveWork,
    isCurrent: isCurrent,
  };
})();
