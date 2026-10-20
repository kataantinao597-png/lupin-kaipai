// 线上写操作真验：登录后点「新建作品」，看作品是否真的落库并出现在列表
const fs = require('fs');
const PORT = 9222;
const BASE = 'https://lupin-kaipai.app.workbuddy.host';
async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 200)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const logs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') logs.push('EXC ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 140));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') logs.push('CONSOLE ' + m.params.args.map(a => a.value ?? a.description).join(' ').slice(0, 140));
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) logs.push(m.params.response.status + ' ' + m.params.response.url.replace(BASE, '').slice(0, 80));
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => (await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise: true })).result.value;

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  await call('Page.navigate', { url: BASE + '/login' });
  await sleep(3000);
  console.log('1) 登录前视图 ->', await ev(`JSON.stringify({loginVisible: !!document.getElementById('login_email') && getComputedStyle(document.getElementById('v-login')).display !== 'none'})`));

  await ev(`(() => {
    const set = (el, v) => { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value'); d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true })); };
    set(document.getElementById('login_email'), 'admin@lupin.test');
    set(document.getElementById('login_password'), 'Lupin@2026');
    document.getElementById('btn_do_auth').click(); return 'ok';
  })()`);
  await sleep(4000);

  const before = await ev(`document.querySelectorAll('.work-card, .work-item, [data-wid]').length`);
  console.log('2) 登录后作品卡片数 =', before);

  const clicked = await ev(`(() => { window.prompt = () => '线上写操作验证作品'; const b = document.getElementById('btn_new_work'); if (!b) return 'NO_BTN'; b.click(); return 'clicked'; })()`);
  console.log('3) 点新建作品 ->', clicked);
  await sleep(4000);

  const after = await ev(`JSON.stringify({
    cards: document.querySelectorAll('.work-card, .work-item, [data-wid]').length,
    toast: (document.querySelector('.toast')||{}).innerText || '',
    activeView: [...document.querySelectorAll('[data-view]')].filter(e=>getComputedStyle(e).display!=='none').map(e=>e.dataset.view),
    workListText: (document.getElementById('v1')||{}).innerText ? document.getElementById('v1').innerText.replace(/\s+/g,' ').slice(0,140) : ''
  })`);
  console.log('4) 新建后 ->', after);
  const shot = await call('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync('D:/workbuddy date/2026-10-18-17-46-13/audit/live-newwork.png', Buffer.from(shot.data, 'base64'));
  console.log('errors:', JSON.stringify([...new Set(logs)].slice(0, 8)));

  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
