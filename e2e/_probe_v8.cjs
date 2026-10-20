// 探针：node _probe_v8.cjs <url> <email> <pwd> <js表达式>
const PORT = +(process.env.CDP_PORT || 9222);
async function http(p, m = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: m });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(t.slice(0, 300)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const connect = (u) => new Promise((res, rej) => { const w = new WebSocket(u); w.onopen = () => res(w); w.onerror = rej; });

(async () => {
  const fsx = require("fs");
  const [url, email, pwd, exprArg] = process.argv.slice(2);
  const expr = fsx.existsSync(exprArg) ? fsx.readFileSync(exprArg, "utf8") : exprArg;
  const t = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pend.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
  const evalJs = async (e) => (await call('Runtime.evaluate', { returnByValue: true, expression: e, awaitPromise: true })).result.value;

  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url });
  await sleep(3000);
  const who = await evalJs(`(document.getElementById('user_name')||{}).textContent`);
  if (who !== email) {
    await evalJs(`(() => { document.getElementById('side_user').click(); return 1; })()`);
    await sleep(700);
    await evalJs(`(() => {
      const set=(el,v)=>{const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');d.set.call(el,v);
        el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
      const e=document.getElementById('login_email'),p=document.getElementById('login_password');
      if(e&&p){set(e,${JSON.stringify(email)});set(p,${JSON.stringify(pwd)});document.getElementById('btn_do_auth').click();}
      return 1;})()`);
    await sleep(4000);
  }
  await evalJs(`(() => { const b=document.querySelector('.nav-item[data-v="v8"]'); if(b) b.click(); return 1; })()`);
  await sleep(2000);
  const raw = await call('Runtime.evaluate', { returnByValue: true, expression: expr, awaitPromise: true });
  if (raw && raw.exceptionDetails) {
    console.log('EXCEPTION ' + JSON.stringify(raw.exceptionDetails.exception || raw.exceptionDetails.text).slice(0, 400));
  }
  const out = raw && raw.result ? raw.result.value : undefined;
  console.log(typeof out === 'string' ? out : JSON.stringify(out, null, 1));
  ws.close();
  await http(`/json/close/${t.id}`);
  setTimeout(() => process.exit(0), 200);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
