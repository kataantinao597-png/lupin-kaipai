// 三跳链路真验：展示页 → 点「开始创作」→ 登录页 → 登录 → 工作台
// node _journey.cjs <email> <pwd> <outPrefix> <w> <h>
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
  const [email, pwd, out, wS, hS] = process.argv.slice(2);
  const width = +wS || 1440, height = +hS || 900;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const logs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') logs.push('EXC ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 160));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') logs.push('CONSOLE ' + m.params.args.map(a => a.value ?? a.description).join(' ').slice(0, 160));
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) logs.push(m.params.response.status + ' ' + m.params.response.url.slice(0, 110));
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const evalJs = async (expr) => (await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise: true })).result.value;
  const shot = async (file) => { const s = await call('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(file, Buffer.from(s.data, 'base64')); };

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });

  // ---- 1. 展示页 ----
  await call('Page.navigate', { url: 'https://lupin-kaipai.app.workbuddy.host/' });
  await sleep(3200);
  const cta = await evalJs(`(() => {
    const a = [...document.querySelectorAll('a')].filter(x => /开始创作|进入工作台|从一款产品开始/.test(x.textContent.trim()));
    return JSON.stringify({ found: a.length, labels: a.map(x=>x.textContent.trim()), hrefs: a.map(x=>x.getAttribute('href')) });
  })()`);
  console.log('1) 展示页 CTA ->', cta);

  // ---- 2. 点主 CTA ----
  const clicked = await evalJs(`(() => {
    const a = [...document.querySelectorAll('a.btn-hero')][0] || [...document.querySelectorAll('a')].find(x=>/开始创作/.test(x.textContent));
    if (!a) return 'NO_CTA';
    a.click(); return 'ok:' + a.textContent.trim();
  })()`);
  console.log('2) 点击 ->', clicked);
  await sleep(2600);
  const st2 = await evalJs(`JSON.stringify({ path: location.pathname, title: document.title, hasForm: !!(document.getElementById('login_email') && document.getElementById('password') || document.getElementById('login_password')) })`);
  console.log('3) 落地 ->', st2);

  // ---- 3. 登录 ----
  const filled = await evalJs(`(() => {
    const set = (el, v) => { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value'); d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); };
    const e = document.getElementById('login_email'), p = document.getElementById('login_password');
    if (!e || !p) return 'NO_FORM';
    set(e, ${JSON.stringify(email)}); set(p, ${JSON.stringify(pwd)});
    document.getElementById('btn_do_auth').click();
    return 'submitted';
  })()`);
  console.log('4) 提交登录 ->', filled);
  await sleep(4000);

  const st3 = await evalJs(`JSON.stringify({
    path: location.pathname,
    visibleViews: [...document.querySelectorAll('[id^="v-"]')].filter(e=>e.offsetParent!==null).map(e=>e.id),
    user: (document.getElementById('user_name')||{}).textContent,
    org: (document.getElementById('user_sub')||{}).textContent,
    loginErr: (document.getElementById('login_err')||{}).textContent,
    err: (document.getElementById('login_err')||{}).textContent
  })`);
  console.log('5) 登录后 ->', st3);
  await shot(out + '-workbench.png');

  // ---- 4. 工作台可用性抽查 ----
  const bench = await evalJs(`JSON.stringify({
    allVisibleViews: [...document.querySelectorAll('[id^="v-"]')].filter(e=>e.offsetParent!==null).map(e=>e.id),
    navItems: [...document.querySelectorAll('.nav-item[data-v]')].map(e=>e.dataset.v),
    hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth,
    bodyText: document.body.innerText.replace(/\\s+/g,' ').slice(0, 160)
  })`);
  console.log('6) 工作台 ->', bench);
  console.log('errors:', JSON.stringify([...new Set(logs)].slice(0, 10)));

  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
