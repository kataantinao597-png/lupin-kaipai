// 通用 CDP 探针：导航 + 滚动预热 + 执行任意表达式，返回 JSON
const fs = require('fs');
const PORT = +(process.env.CDP_PORT || 9222);

async function http(path, method = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${path}`, { method });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(`${method} ${path} -> ${t.slice(0, 200)}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function connect(wsUrl) {
  return new Promise((res, rej) => {
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => res(ws);
    ws.onerror = (e) => rej(e);
  });
}

(async () => {
  const [url, exprFile, wStr, hStr] = process.argv.slice(2);
  const width = parseInt(wStr || '1440', 10);
  const height = parseInt(hStr || '900', 10);
  const expression = fs.readFileSync(exprFile, 'utf8');

  const target = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const myId = ++id;
    pending.set(myId, { resolve, reject });
    ws.send(JSON.stringify({ id: myId, method, params }));
  });

  await call('Page.enable');
  await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url });
  await sleep(3500);

  await call('Runtime.evaluate', {
    expression: `(async () => {
      const h = document.documentElement.scrollHeight;
      for (let y = 0; y < h; y += 500) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
      window.scrollTo(0, 0);
    })()`,
    awaitPromise: true,
  });
  await sleep(1500);

  const r = await call('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  });
  console.log(typeof r.result.value === 'string' ? r.result.value : JSON.stringify(r.result.value ?? r, null, 2));
  if (r.exceptionDetails) console.log('EXCEPTION', JSON.stringify(r.exceptionDetails).slice(0, 500));

  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
