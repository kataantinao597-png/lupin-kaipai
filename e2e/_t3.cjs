// probe
const fs = require('fs');
const OUT = '_p2.json';
const W = (o) => fs.writeFileSync(OUT, JSON.stringify(o, null, 1));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const r = await fetch('http://127.0.0.1:9222/json/new?url=about:blank', { method: 'PUT' });
  const t = await r.json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = () => res(); ws.onerror = () => rej(new Error('ws err')); });
  let id = 0; const pend = new Map(); const netlog = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Network.responseReceived') {
      const rr = m.params.response;
      if (/\/api\//.test(rr.url)) netlog.push(rr.status + ' ' + rr.url.replace(/^https?:\/\/[^/]+/, ''));
    }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => {
    const x = await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise: true });
    return x.result && x.result.value;
  };

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url: 'https://lupin-kaipai.app.workbuddy.host/app' });
  await sleep(4500);

  const clickRes = await ev(`(() => {
    const set = (el, v) => {
      const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      d.set.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const e = document.getElementById('login_email'), p = document.getElementById('login_password');
    if (!e || !p) return 'NO_FORM';
    set(e, 'admin@lupin.test'); set(p, 'Lupin@2026');
    document.getElementById('btn_do_auth').click();
    return 'CLICKED';
  })()`);
  await sleep(5000);

  const afterLogin = await ev(`JSON.stringify({
    hash: location.hash, path: location.pathname,
    loginErr: (document.getElementById('login_err')||{}).textContent,
    navs: [...document.querySelectorAll('.nav-item')].map(e => e.dataset.v)
  })`);

  await ev(`(() => { const n = document.querySelector('.nav-item[data-v="v4"]'); if (n) n.click(); return !!n; })()`);
  await sleep(3500);

  const state = await ev(`JSON.stringify({
    IMG_CONFIGURED: (typeof IMG_CONFIGURED !== 'undefined') ? IMG_CONFIGURED : 'undef',
    IMG_QUOTA: (typeof IMG_QUOTA !== 'undefined') ? IMG_QUOTA : 'undef',
    hint: (document.getElementById('ai_img_status')||{}).textContent,
    btnDisabled: (document.getElementById('btn_gen_poster')||{}).disabled,
    v4visible: !!document.getElementById('v4') && document.getElementById('v4').offsetParent !== null
  })`);

  W({ clickRes, afterLogin, state, net: netlog.slice(-30) });
  ws.close();
  setTimeout(() => process.exit(0), 300);
})().catch((e) => { W({ err: String(e) }); process.exit(1); });
