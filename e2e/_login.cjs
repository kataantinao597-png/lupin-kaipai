// 真实 UI 登录流程：node _login.cjs <url> <out> <w> <h> <email> <pwd>
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
  const [url, out, wS, hS, email, pwd] = process.argv.slice(2);
  const width = +wS || 1440, height = +hS || 1000;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const logs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') logs.push('EXC ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 200));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') logs.push('CONSOLE ' + m.params.args.map(a => a.value ?? a.description).join(' ').slice(0, 200));
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) logs.push(m.params.response.status + ' ' + m.params.response.url);
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url });
  await sleep(3000);

  const r1 = await call('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const set = (el, v) => {
        const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        d.set.call(el, v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const e = document.getElementById('login_email'), p = document.getElementById('login_password');
      if (!e || !p) return 'NO_FORM';
      set(e, ${JSON.stringify(email)}); set(p, ${JSON.stringify(pwd)});
      document.getElementById('btn_do_auth').click();
      return 'CLICKED';
    })()`,
  });
  console.log('step1:', r1.result.value);
  await sleep(3500);

  const state = await call('Runtime.evaluate', {
    returnByValue: true,
    expression: `JSON.stringify({
      hash: location.hash, path: location.pathname,
      visible: [...document.querySelectorAll('[id^="v-"], [data-view]')].filter(e=>e.offsetParent!==null).map(e=>e.id||e.dataset.view),
      loginErr: (document.getElementById('login_err')||{}).textContent,
      hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth
    })`,
  });
  console.log('state:', state.result.value);

  const shot = await call('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
  console.log('shot:', out);
  console.log('errors:', JSON.stringify([...new Set(logs)].slice(0, 12)));
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
