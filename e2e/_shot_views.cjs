// 指定视图截图：node _shot_views.cjs <url> <w> <h> <email> <pwd> <outdir> <v1,v2,...>
const fs = require('fs');
const path = require('path');
const PORT = +(process.env.CDP_PORT || 9222);
async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 300)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const [url, wS, hS, email, pwd, outdir, list] = process.argv.slice(2);
  const width = +wS, height = +hS;
  const views = list.split(',');
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const evalJs = async (expr) => (await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise: true })).result.value;

  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  await call('Page.navigate', { url });
  await sleep(3000);

  const who = await evalJs(`(document.getElementById('user_name')||{}).textContent`);
  if (who !== email) {
    await evalJs(`(() => { const u = document.getElementById('side_user'); if (u) u.click(); return 1; })()`);
    await sleep(700);
    await evalJs(`(() => {
      const set = (el, v) => { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value'); d.set.call(el,v);
        el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); };
      const e = document.getElementById('login_email'), p = document.getElementById('login_password');
      if (e && p) { set(e, ${JSON.stringify(email)}); set(p, ${JSON.stringify(pwd)}); document.getElementById('btn_do_auth').click(); }
      return 1; })()`);
    await sleep(4000);
  }

  fs.mkdirSync(outdir, { recursive: true });
  for (const v of views) {
    await evalJs(`(() => { const b = document.querySelector('.sidebar .nav-item[data-v="${v}"]'); if (b) b.click(); return 1; })()`);
    await sleep(1000);
    const h = await evalJs(`(() => { const s = document.getElementById('${v}'); return s ? Math.round(s.getBoundingClientRect().height) : -1; })()`);
    // 滚到顶部再截视口（整页截图对远离视口的区块会错绘）
    await evalJs(`window.scrollTo(0,0)`);
    await sleep(400);
    const shot = await call('Page.captureScreenshot', { format: 'png' });
    const f = path.join(outdir, `${width}-${v}.png`);
    fs.writeFileSync(f, Buffer.from(shot.data, 'base64'));
    console.log(`${v} h=${h} -> ${f}`);
  }
  ws.close();
  await http(`/json/close/${t.id}`);
  setTimeout(() => process.exit(0), 200);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
