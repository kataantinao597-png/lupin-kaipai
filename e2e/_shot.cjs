// 用 CDP 抓整页截图 + 控制台错误（Node 22 自带 WebSocket）
const fs = require('fs');

const PORT = 9222;

async function http(path, method = 'GET') {
  const r = await fetch(`http://127.0.0.1:${PORT}${path}`, { method });
  const t = await r.text();
  try { return JSON.parse(t); } catch { throw new Error(`${method} ${path} -> ${t.slice(0, 200)}`); }
}

function connect(wsUrl) {
  return new Promise((res, rej) => {
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => res(ws);
    ws.onerror = (e) => rej(e);
  });
}

function rpc(ws) {
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  };
  return {
    events,
    call(method, params = {}) {
      const myId = ++id;
      return new Promise((resolve, reject) => {
        pending.set(myId, { resolve, reject });
        ws.send(JSON.stringify({ id: myId, method, params }));
      });
    },
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const [url, out, wStr, hStr] = process.argv.slice(2);
  const width = parseInt(wStr || '1440', 10);
  const height = parseInt(hStr || '900', 10);

  const target = await http('/json/new?url=about:blank', 'PUT');
  const ws = await connect(target.webSocketDebuggerUrl);
  const c = rpc(ws);

  const consoleErrors = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      consoleErrors.push(m.params.entry.text);
    }
    if (m.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(m.params.exceptionDetails.text + ' ' + (m.params.exceptionDetails.exception?.description || ''));
    }
    if (m.method === 'Network.loadingFailed') {
      consoleErrors.push('LOAD_FAIL ' + m.params.errorText);
    }
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) {
      consoleErrors.push(m.params.response.status + ' ' + m.params.response.url);
    }
  });

  await c.call('Page.enable');
  await c.call('Log.enable');
  await c.call('Runtime.enable');
  await c.call('Network.enable');
  await c.call('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile: false,
  });
  await c.call('Page.navigate', { url });
  await sleep(3500);

  // 滚动预热：触发 IntersectionObserver 入场动画，避免 reveal(opacity:0) 元素截图全空
  await c.call('Runtime.evaluate', {
    expression: `(async () => {
      const h = document.documentElement.scrollHeight;
      for (let y = 0; y < h; y += 600) {
        window.scrollTo(0, y);
        await new Promise(r => setTimeout(r, 90));
      }
      window.scrollTo(0, 0);
    })()`,
    awaitPromise: true,
  });
  await sleep(1200);

  const metrics = await c.call('Page.getLayoutMetrics');
  const content = metrics.cssContentSize || metrics.contentSize;
  const fullH = Math.min(Math.ceil(content.height), 12000);

  const shot = await c.call('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
    clip: { x: 0, y: 0, width, height: fullH, scale: 1 },
  });
  fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));

  // 横向滚动检测
  const hscroll = await c.call('Runtime.evaluate', {
    expression: 'JSON.stringify({scrollW:document.documentElement.scrollWidth,clientW:document.documentElement.clientWidth,scrollH:document.documentElement.scrollHeight})',
    returnByValue: true,
  });

  console.log(JSON.stringify({
    url, out, contentH: fullH, layout: JSON.parse(hscroll.result.value),
    errors: [...new Set(consoleErrors)].slice(0, 15),
  }, null, 2));

  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`);
  process.exit(0);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
