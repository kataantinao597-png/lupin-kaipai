/* 鲁品开拍 - 工作台 DOM 冒烟测试（v3 架构）
 *
 * 不依赖后端、不调用 AI：验证
 *  1. 页面可加载、init() 无后端时落到公开首页 v0
 *  2. 登录守卫：未登录访问 v1..v6 被带到 v-login
 *  3. 核心表单/按钮元素存在（产品资料、创意、分镜、海报、交付）
 *  4. doLogout() 存在且登出跳转到 /
 *
 * 需要 AI 的深度流程由 pytest 后端用例与 Playwright 真浏览器测试覆盖。
 * 运行：node e2e/e2e-jsdom.js
 */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const FE = '/home/hatch/workspace/lupin-kaipai/frontend';
const results = [];
const shot = (n, ok, d = '') => { results.push(!!ok); console.log(`${ok ? 'PASS' : 'FAIL'} ${n} ${d}`); };

(async () => {
  const html = fs.readFileSync(path.join(FE, 'app.html'), 'utf8');
  const apiJs = fs.readFileSync(path.join(FE, 'js', 'services', 'api-client.js'), 'utf8');
  const authJs = fs.readFileSync(path.join(FE, 'js', 'services', 'auth-client.js'), 'utf8');
  const appJs = fs.readFileSync(path.join(FE, 'js', 'app.js'), 'utf8');

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8931/app',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;

  // 补浏览器 API
  window.confirm = () => true;
  window.prompt = () => '';
  window.alert = () => {};
  window.IntersectionObserver = class {
    constructor(cb) { this.cb = cb; }
    observe(t) { this.cb([{ isIntersecting: true, target: t }]); }
    unobserve() {}
    disconnect() {}
  };
  // 无后端：fetch 直接失败，走 catch 分支
  window.fetch = () => Promise.reject(new Error('no backend in smoke test'));
  window.scrollTo = () => {};
  // location.href 跳转改为记录（jsdom 不真跳）
  let navigatedTo = null;
  try {
    Object.defineProperty(window, 'location', {
      value: new Proxy(window.location, {
        set(o, k, v) { if (k === 'href') { navigatedTo = v; return true; } o[k] = v; return true; },
      }),
      configurable: true,
    });
  } catch (e) { /* 旧 jsdom 不支持则跳过 */ }

  window.eval(apiJs + '\n' + authJs + '\n' + appJs);

  const $ = (id) => window.document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const activeView = () => {
    const el = window.document.querySelector('.view.active');
    return el ? el.id : '(none)';
  };

  try {
    // 1. init()：无后端 -> tryRefresh 失败 -> 落到公开首页 v0
    window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
    await sleep(1500);
    shot('init 无后端时显示公开首页', activeView() === 'v0', `当前=${activeView()}`);

    // 2. 登录守卫：v1..v6 未登录全部被带到 v-login
    let guardOk = true;
    for (const v of ['v1', 'v2', 'v3', 'v4', 'v5', 'v6']) {
      window.go(v);
      await sleep(120);
      if (activeView() !== 'v-login') { guardOk = false; break; }
    }
    shot('未登录访问受保护视图均回登录页', guardOk);

    // 3. 公开视图可达
    window.go('v0');
    await sleep(120);
    shot('公开首页 v0 可达', activeView() === 'v0');

    // 4. 核心表单与按钮存在
    const needIds = [
      'login_email', 'login_password', 'btn_do_auth', 'btn_logout',
      'f_content', 'f_category', 'f_source',
      'btn_gen_ideas', 'btn_gen_storyboard',
      'btn_gen_poster', 'btn_export_all', 'btn_delivery_check',
    ];
    const missing = needIds.filter((id) => !$(id));
    shot('核心表单/按钮元素存在', missing.length === 0, missing.length ? `缺失:${missing.join(',')}` : `${needIds.length}个`);

    // 5. 登录视图结构完整（登录/注册切换）
    window.go('v-login');
    await sleep(120);
    shot('登录视图含登录/注册切换', !!($('tab_login') && $('tab_register') && $('btn_do_auth')));

    // 6. 登出接线存在（btn_logout -> doLogout；实际跳转由 Playwright 真浏览器覆盖）
    const wired = appJs.includes('btn_logout').toString && /btn_logout"\)\.addEventListener\("click",\s*doLogout/.test(appJs);
    shot('登出按钮已接线 doLogout', !!($('btn_logout') && wired));
  } catch (e) {
    shot('冒烟流程', false, e.message.slice(0, 160));
  }

  const fails = results.filter((r) => !r).length;
  console.log(`\n总计: ${results.length - fails}/${results.length} 通过`);
  process.exit(fails ? 1 : 0);
})();
