// V3.0 视图审计：node _v3_audit.cjs <url> <w> <h> <email> <pwd> [outdir]
// 逐视图测量：横向溢出、可见卡片数、Console/异常/HTTP>=400
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
  const [url, wS, hS, email, pwd, outdir] = process.argv.slice(2);
  const width = +wS, height = +hS;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const logs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); return; }
    if (m.method === 'Runtime.exceptionThrown') logs.push('EXC ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 180));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') logs.push('CONSOLE ' + m.params.args.map(a => a.value ?? a.description).join(' ').slice(0, 180));
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) logs.push(m.params.response.status + ' ' + m.params.response.url.replace(url.replace(/\/$/, ''), ''));
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const evalJs = async (expr, awaitPromise = false) => {
    const r = await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise });
    return r.result.value;
  };

  await call('Page.enable'); await call('Runtime.enable'); await call('Network.enable');
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  await call('Page.navigate', { url });
  await sleep(3000);

  // ---- 登录（先点侧栏用户区强制进登录页，再填表提交）----
  await evalJs(`(() => { const u = document.getElementById('side_user'); if (u) u.click(); return 1; })()`);
  await sleep(700);
  const loginState = await evalJs(`(async () => {
    const set = (el, v) => {
      const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      d.set.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const e = document.getElementById('login_email'), p = document.getElementById('login_password');
    if (!e || !p) return 'NO_FORM';
    if (!document.getElementById('v-login').classList.contains('active')) return 'NOT_ON_LOGIN';
    set(e, ${JSON.stringify(email)}); set(p, ${JSON.stringify(pwd)});
    document.getElementById('btn_do_auth').click();
    return 'SUBMITTED';
  })()`, true);
  await sleep(4500);
  const afterLogin = await evalJs(`JSON.stringify({
    activeView: (document.querySelector('.view.active')||{}).id,
    loginErr: (document.getElementById('login_err')||{}).textContent || '',
    user: (document.getElementById('user_name')||{}).textContent || ''
  })`);
  console.log(`[login] ${loginState} -> ${afterLogin}`);

  // ---- 导航项（含 JS 动态注入的 v7/v8/v9）----
  const navs = JSON.parse(await evalJs(
    `JSON.stringify([...document.querySelectorAll('.sidebar .nav-item[data-v]')]
       .map(b => ({ v: b.dataset.v, t: b.textContent.trim() })))`));
  console.log(`[nav] ${navs.length} 项: ${navs.map(n => n.v + ':' + n.t).join(' | ')}`);

  const rows = [];
  for (const n of navs) {
    await evalJs(`(() => { const b = document.querySelector('.sidebar .nav-item[data-v="${n.v}"]'); if (b) b.click(); return 1; })()`);
    await sleep(900);
    const m = JSON.parse(await evalJs(`(() => {
      const de = document.documentElement;
      const v = document.getElementById('${n.v}');
      const over = [];
      if (v) v.querySelectorAll('*').forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width === 0 || b.height === 0) return;
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed') return;
        if (b.right > de.clientWidth + 1.5 || b.left < -1.5) {
          over.push(el.tagName.toLowerCase() + '.' + (el.className || '').toString().split(' ').slice(0, 2).join('.')
            + '@' + Math.round(b.left) + '..' + Math.round(b.right));
        }
      });
      return JSON.stringify({
        active: v ? v.classList.contains('active') : false,
        h: v ? Math.round(v.getBoundingClientRect().height) : 0,
        cards: v ? v.querySelectorAll('.card').length : 0,
        hscroll: de.scrollWidth > de.clientWidth + 1,
        scrollW: de.scrollWidth, clientW: de.clientWidth,
        over: over.slice(0, 5), overTotal: over.length
      });
    })()`));
    rows.push({ ...n, ...m });
    const flag = m.hscroll ? 'HSCROLL' : 'ok';
    console.log(`  ${n.v.padEnd(4)} ${n.t.padEnd(6)} ${flag.padEnd(8)} active=${m.active ? 1 : 0} h=${String(m.h).padStart(4)} cards=${String(m.cards).padStart(2)} over=${m.overTotal} ${m.overTotal ? JSON.stringify(m.over.slice(0, 3)) : ''}`);
    if (outdir && (n.v === 'v0' || m.hscroll)) {
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(outdir, { recursive: true });
      fs.writeFileSync(path.join(outdir, `${width}-${n.v}.png`), Buffer.from(shot.data, 'base64'));
    }
  }

  // ---- 移动端抽屉 ----
  if (width <= 760) {
    const drawer = JSON.parse(await evalJs(`(async () => {
      const btn = document.getElementById('btn_nav');
      const side = document.getElementById('sidebar');
      if (!btn) return JSON.stringify({ ok: false, why: 'no btn_nav' });
      const before = getComputedStyle(side).transform;
      btn.click();
      await new Promise(r => setTimeout(r, 450));
      const opened = document.body.classList.contains('nav-open');
      const after = getComputedStyle(side).transform;
      const bd = document.getElementById('nav_backdrop');
      const bdOp = bd ? getComputedStyle(bd).opacity : 'n/a';
      const r = side.getBoundingClientRect();
      // 点一个导航项应收起
      const item = side.querySelector('.nav-item[data-v]');
      if (item) item.click();
      await new Promise(r => setTimeout(r, 450));
      const closed = !document.body.classList.contains('nav-open');
      return JSON.stringify({ ok: true, opened, closed, bdOp, x: Math.round(r.left), w: Math.round(r.width), before, after });
    })()`, true));
    console.log(`[drawer] ${JSON.stringify(drawer)}`);
  }

  const uniq = [...new Set(logs)].filter(l => !/favicon/i.test(l));
  console.log(`[errors] ${uniq.length}`);
  uniq.slice(0, 10).forEach(l => console.log('   ' + l));

  ws.close();
  await http(`/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
