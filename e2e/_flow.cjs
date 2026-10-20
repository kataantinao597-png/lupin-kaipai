// 登录后按视图截图：node _flow.cjs <out-prefix> <w> <h> <email> <pwd> <v0,v2,v3,...>
const fs = require('fs');
const PORT = 9222;
async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 200)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const [pre, wS, hS, email, pwd, views] = process.argv.slice(2);
  const width = +wS, height = +hS;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const errs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') errs.push('EXC ' + (m.params.exceptionDetails.exception?.description || '').slice(0, 160));
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 500) errs.push(m.params.response.status + ' ' + m.params.response.url);
  };
  const call = (m, p = {}) => new Promise((res2, rej) => { const i = ++id; pend.set(i, { resolve: res2, reject: rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
  const ev = (expression, awaitPromise = false) => call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  await call('Page.navigate', { url: 'http://127.0.0.1:8080/login' });
  await sleep(2600);

  // 登录
  await ev(`(() => {
    const set = (el, v) => { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value'); d.set.call(el,v);
      el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); };
    set(document.getElementById('login_email'), ${JSON.stringify(email)});
    set(document.getElementById('login_password'), ${JSON.stringify(pwd)});
    document.getElementById('btn_do_auth').click();
    return 'ok';
  })()`);
  await sleep(3500);

  for (const v of views.split(',').filter(Boolean)) {
    const r = await ev(`(() => {
      const el = document.querySelector('.nav-item[data-v="${v}"]');
      if (el) { el.click(); return 'clicked'; }
      if (typeof go === 'function') { go('${v}'); return 'go()'; }
      return 'NOT_FOUND';
    })()`);
    await sleep(2200);
    const info = await ev(`(() => {
      const sec = document.getElementById('${v}');
      if (!sec) return 'no-view';
      const vis = sec.classList.contains('active');
      const txt = (sec.innerText || '').replace(/\\s+/g, ' ').trim();
      return JSON.stringify({ active: vis, len: txt.length, head: txt.slice(0, 180) });
    })()`);
    const shot = await call('Page.captureScreenshot', { format: 'png' });
    const p = `${pre}-${v}.png`;
    fs.writeFileSync(p, Buffer.from(shot.data, 'base64'));
    console.log(`${v} [${r.result.value}] ${info.result.value} -> ${p}`);
  }

  console.log('errors:', JSON.stringify([...new Set(errs)].slice(0, 8)));
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
