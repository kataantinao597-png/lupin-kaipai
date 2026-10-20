// 滚到指定 y 后截当前视口（captureBeyondViewport=false，最可信）
// node _viewp.cjs <url> <out> <w> <h> <y>
const fs = require('fs');
const PORT = +(process.env.CDP_PORT || 9222);

async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 200)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const [url, out, wS, hS, yS] = process.argv.slice(2);
  const width = +wS, height = +hS, y = +yS;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const i = ++id; pend.set(i, { resolve, reject });
    ws.send(JSON.stringify({ id: i, method, params }));
  });

  await call('Page.enable');
  await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride',
    { width, height, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url });
  await sleep(2500);
  await call('Runtime.evaluate', {
    expression: `window.scrollTo(0, ${y}); 1`, returnByValue: true,
  });
  await sleep(700);
  const shot = await call('Page.captureScreenshot',
    { format: 'png', captureBeyondViewport: false });
  fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
  console.log('saved', out, 'at y=' + y);
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
