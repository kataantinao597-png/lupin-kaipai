// 检查海报页生图按钮的真实禁用状态
const PORT = 9222;
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
  let id = 0; const pend = new Map();
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); } };
  const call = (m, p = {}) => new Promise((res2, rej) => { const i = ++id; pend.set(i, { resolve: res2, reject: rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
  const ev = (e) => call('Runtime.evaluate', { expression: e, returnByValue: true });

  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url: 'http://127.0.0.1:8080/login' });
  await sleep(2600);
  await ev(`(() => { const set=(el,v)=>{const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');d.set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
    set(document.getElementById('login_email'),'demo@lupin.test'); set(document.getElementById('login_password'),'demo1234');
    document.getElementById('btn_do_auth').click(); return 1; })()`);
  await sleep(3500);
  await ev(`document.querySelector('.nav-item[data-v="v4"]').click()`);
  await sleep(2500);
  const r = await ev(`(() => {
    const b = document.getElementById('btn_gen_poster');
    if (!b) return 'no-btn';
    const cs = getComputedStyle(b);
    return JSON.stringify({
      disabled: b.disabled, hasClass: b.classList.contains('qa-disabled'),
      opacity: cs.opacity, cursor: cs.cursor,
      styleFit: JSON.stringify({op:b.style.opacity, cls:b.className}),
      hint: (document.getElementById('ai_img_status')||{}).textContent
    });
  })()`);
  console.log(r.result.value);
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${t.id}`);
  process.exit(0);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
