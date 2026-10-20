// 横向溢出审计：node _audit.cjs <url> <w> <h>
const PORT = (process.env.CDP_PORT || 9222);
async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 200)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const [url, wS, hS] = process.argv.slice(2);
  const width = +wS, height = +hS;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); } };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });

  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  await call('Page.navigate', { url });
  await sleep(2800);
  await call('Runtime.evaluate', {
    expression: `(async()=>{const h=document.documentElement.scrollHeight;for(let y=0;y<h;y+=400){window.scrollTo(0,y);await new Promise(r=>setTimeout(r,60));}window.scrollTo(0,0);})()`,
    awaitPromise: true,
  });
  await sleep(600);

  const r = await call('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const de = document.documentElement;
      const over = [];
      document.querySelectorAll('body *').forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width === 0 || b.height === 0) return;
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed') return;
        if (b.right > de.clientWidth + 1.5 || b.left < -1.5) {
          over.push({ sel: el.tagName.toLowerCase() + '.' + (el.className||'').toString().split(' ').slice(0,2).join('.'),
                      left: Math.round(b.left), right: Math.round(b.right), w: Math.round(b.width) });
        }
      });
      return JSON.stringify({
        vw: de.clientWidth, scrollW: de.scrollWidth,
        hscroll: de.scrollWidth > de.clientWidth + 1,
        overflowers: over.slice(0, 8), total: over.length
      });
    })()`,
  });
  const v = JSON.parse(r.result.value);
  console.log(`w=${width} ${v.hscroll ? 'HSCROLL' : 'ok'} scrollW=${v.scrollW} clientW=${v.vw} overflowers=${v.total} ${v.total ? JSON.stringify(v.overflowers.slice(0,4)) : ''}`);
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
