(async () => {
  const keys = [];
  const orig = window.fetch;
  window.fetch = function (u, o) {
    try { keys.push({ url: String(u), key: (o && o.headers) ? o.headers["Idempotency-Key"] : null }); }
    catch (e) { keys.push({ url: "?", key: "ERR" }); }
    return Promise.reject(new Error("blocked-by-test"));
  };
  const call = async (body) => { try { await ApiClient.post("/api/ideas", body, { idempotencyKey: "auto" }); } catch (e) { /* 期望失败 */ } };
  await call({ product: { product_name: "x" }, org_id: "o" });
  await call({ product: { product_name: "x" }, org_id: "o" });   // 同一请求体，2 秒内 → 应复用 key
  await call({ product: { product_name: "y" }, org_id: "o" });   // 不同请求体 → 应换 key
  window.fetch = orig;
  return JSON.stringify({
    n: keys.length,
    k0: keys[0] && keys[0].key,
    k1: keys[1] && keys[1].key,
    k2: keys[2] && keys[2].key,
    same01: keys[0] && keys[1] && keys[0].key === keys[1].key,
    diff12: keys[1] && keys[2] && keys[1].key !== keys[2].key,
    url0: keys[0] && keys[0].url
  });
})()
