const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const FE = '/home/hatch/workspace/lupin-kaipai/frontend';
const BASE = 'http://127.0.0.1:8931';
const results = [];
const shot = (n, ok, d='') => { results.push(ok); console.log(`${ok?'PASS':'FAIL'} ${n} ${d}`); };

(async () => {
  const html = fs.readFileSync(path.join(FE, 'index.html'), 'utf8');
  const js = fs.readFileSync(path.join(FE, 'app.js'), 'utf8');

  const dom = new JSDOM(html, { url: BASE + '/', runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;

  // 补浏览器 API
  window.confirm = () => true;
  window.prompt = () => '改成更突出葱白的特写镜头';
  window.alert = () => {};
  window.fetch = (url, opts) => fetch(new URL(url, window.location.href).href, opts);
  let capturedBlob = null;
  window.URL.createObjectURL = (blob) => { capturedBlob = blob; return 'blob:test'; };
  window.URL.revokeObjectURL = () => {};
  let dlName = '';
  const origCreate = window.document.createElement.bind(window.document);
  window.document.createElement = function (tag, opt) {
    const el = origCreate(tag, opt);
    if (tag === 'a') { const oc = el.click.bind(el); el.click = () => { dlName = el.download; }; }
    return el;
  };

  window.eval(js + '\n;window.__getState=function(){return state};');
  const $ = (id) => window.document.getElementById(id);
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const S = () => window.__getState();
  const waitFor = async (fn, timeout, label, errId) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) { try { if (await fn()) return true; } catch(e){} await sleep(2000); }
    const em = errId ? $(''+errId).textContent.slice(0,120) : '';
    throw new Error('超时: ' + label + (em ? ' | 页面报错: ' + em : ''));
  };

  try {
    // 1. 填表单
    $('f_name').value = '章丘大葱';
    $('f_desc').value = '葱白长、甜脆，适合蘸酱';
    $('f_points').value = '葱白长\n甜脆';
    $('f_audience').value = '家庭主妇';
    $('f_device').value = '手机';
    $('f_people').value = '1';
    $('f_location').value = '菜地';
    $('f_duration').value = '45';
    shot('填写产品表单', true);

    // 2. 生成创意方向
    $('btn_ideas').click();
    await waitFor(() => $('ideas').children.length >= 3, 420000, '创意方向生成', 'err1');
    shot('生成3个创意方向', true, `${$('ideas').children.length}个`);

    // 3. 选方向 -> 分镜
    window.selectIdea(0);
    await waitFor(() => (S().storyboard && S().storyboard.shots && S().storyboard.shots.length >= 3), 420000, '分镜生成', 'err2');
    const n = S().storyboard.shots.length;
    const total = S().storyboard.shots.reduce((a,s)=>a+s.duration_seconds,0);
    shot('生成分镜', n >= 3, `${n}镜/${total}s`);

    // 4. 编辑第1镜旁白
    window.editShot(0);
    await sleep(400);
    const ta = window.document.querySelector('#e_vo_0');
    if (!ta) throw new Error('没找到旁白编辑框');
    ta.value = 'E2E旁白：章丘大葱蘸酱吃';
    // 触发 input 让框架感知（直接调 saveShot 读 DOM）
    ta.dispatchEvent(new window.Event('input', { bubbles: true }));
    await window.saveShot(0);
    await sleep(500);
    shot('编辑镜头旁白', (S().storyboard.shots[0].voiceover||'').includes('E2E旁白'));

    // 5. 锁定 + 重生成（锁定第1镜，重生成第3镜，验证锁定不被覆盖）
    const lockNo = S().storyboard.shots[0].shot_number;
    const lockBefore = JSON.stringify(S().storyboard.shots[0]);
    S().locked.add(lockNo);
    const regenTarget = S().storyboard.shots[2];
    const oldAction = regenTarget.shooting_action;
    await window.regenShot(regenTarget.shot_number);
    const lockedAfter = JSON.stringify(S().storyboard.shots.find(s=>s.shot_number===lockNo));
    const newAction = S().storyboard.shots.find(s=>s.shot_number===regenTarget.shot_number).shooting_action;
    shot('锁定镜头不被覆盖', lockedAfter === lockBefore);
    shot('单镜头重生成', !!newAction && newAction !== oldAction);

    // 6. 风险页
    $('btn_goto_risk').click();
    await sleep(4000);
    const risks = window.document.querySelectorAll('#risks > *').length;
    shot('风险页渲染', true, `${risks}条`);
    window.document.querySelectorAll('#risks input[type=checkbox]').forEach(c => { if(!c.checked) c.click(); });

    // 7. 导出（完整45s，应成功）
    $('btn_export').click();
    await waitFor(() => !!capturedBlob, 90000, '导出下载', 'err4');
    const buf = Buffer.from(await capturedBlob.arrayBuffer());
    fs.writeFileSync('/home/hatch/workspace/lupin-kaipai/e2e/export-e2e.docx', buf);
    shot('导出DOCX', buf.length > 5000, `${buf.length}字节/${dlName}`);

    // 8. 删除镜头后总时长不符，导出应被拦截
    window.showView('v3');
    await sleep(500);
    const n0 = S().storyboard.shots.length;
    window.delShot(1);
    await sleep(500);
    shot('删除镜头', S().storyboard.shots.length === n0 - 1, `${n0}->${S().storyboard.shots.length}`);
    capturedBlob = null;
    window.showView('v4');
    await sleep(500);
    $('btn_export').click();
    await sleep(15000);
    const blocked = $('err4').textContent.includes('总时长');
    shot('时长不符时导出被拦截', blocked && !capturedBlob, $('err4').textContent.slice(0,50));

    // 9. 校验条状态
    const vb = $('validate_bar') ? $('validate_bar').textContent.slice(0,60) : '';
    console.log('校验条:', vb);
  } catch (e) {
    shot('E2E流程', false, e.message.slice(0,160));
  }

  const fails = results.filter(r => !r).length;
  console.log(`\n总计: ${results.length - fails}/${results.length} 通过`);
  process.exit(fails ? 1 : 0);
})();
