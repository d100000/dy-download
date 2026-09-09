const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');

const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('/* ================= 登录 / 注册');
const end = html.indexOf('\nrefreshUser();', start);
const settle = () => new Promise(resolve => setImmediate(resolve));
const reply = (data, status = 200) => ({ok: status < 400, status, json: async () => data});

function harness(handleRequest) {
  const nodes = new Map();
  function $(key) {
    if (!nodes.has(key)) nodes.set(key, {
      value: '', textContent: '', disabled: false, dataset: {}, style: {}, attrs: {},
      classList: {add() {}, remove() {}, toggle() {}},
      addEventListener() {}, focus() {},
      setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
      getBoundingClientRect() { return {width: 300}; },
      click() { return this.onclick(); },
    });
    return nodes.get(key);
  }
  const calls = [];
  const timers = [];
  const context = {
    $, LANG: 'zh', t: () => null, _loggedIn: false, refreshQuota() {},
    showError(message) { $('#error').textContent = message; },
    TextEncoder, setTimeout: fn => { timers.push(fn); },
    window: {addEventListener() {}},
    document: {body: {style: {}}, addEventListener() {}, querySelectorAll: () => []},
    fetch: async (path, options = {}) => {
      calls.push({path, options});
      const custom = await handleRequest?.(path, options);
      if (custom) return custom;
      if (path === '/api/auth/math') return reply({cid: 'question', question: '3 + 4 = ?'});
      if (path === '/api/auth/math/verify') return reply({math_token: 'math-proof'});
      if (path === '/api/auth/captcha') return reply({cid: 'slider', bg: 'bg', piece: 'piece', y: 20, h: 170, w: 300, piece_size: 50, pow_bits: 0});
      if (path === '/api/auth/captcha/verify') return reply({pass_token: 'slider-proof'});
      if (path === '/api/auth/login' || path === '/api/auth/register') return reply({ok: true});
      throw new Error('Unexpected request: ' + path);
    },
  };
  assert.ok(start >= 0 && end > start);
  vm.runInNewContext(html.slice(start, end) + '\nthis.auth = {openAuth, closeAuth, setMode, verifyCaptcha};', context);
  async function open(mode = 'login') {
    context.auth.openAuth(mode);
    await settle();
    $('#authEmail').value = 'test@example.test';
    $('#authPw').value = 'test-password';
  }
  return {$, calls, timers, context, open};
}

test('empty or incorrect arithmetic never loads the slider and allows another attempt', async () => {
  const h = harness(path => path === '/api/auth/math/verify' ? reply({error: '答案不正确'}, 400) : null);
  await h.open();
  await h.$('#authNext').click();
  assert.match(h.$('#authErr').textContent, /算术题/);
  h.$('#authMathAnswer').value = '99';
  await h.$('#authNext').click();
  assert.equal(h.calls.filter(c => c.path === '/api/auth/captcha').length, 0);
  assert.equal(h.$('#authStep2').style.display, 'none');
  assert.equal(h.$('#authMathAnswer').value, '');
  assert.equal(h.$('#authNext').disabled, false);
  assert.equal(h.$('#authErr').textContent, '答案不正确');
});

test('both login and registration require math then slider before submitting credentials', async () => {
  for (const mode of ['login', 'register']) {
    const h = harness();
    await h.open(mode);
    h.$('#authMathAnswer').value = '7';
    await h.$('#authNext').click();
    assert.deepEqual(h.calls.map(c => c.path), ['/api/auth/math', '/api/auth/math/verify', '/api/auth/captcha']);
    assert.equal(h.calls[2].options.headers['X-Auth-Math'], 'math-proof');
    assert.equal(h.$('#authStep2').style.display, '');
    await h.context.auth.verifyCaptcha();
    const last = h.calls.at(-1);
    assert.equal(last.path, '/api/auth/' + mode);
    assert.equal(JSON.parse(last.options.body).pass_token, 'slider-proof');
    assert.equal(JSON.parse(last.options.body).email, 'test@example.test');
    assert.equal(last.options.credentials, 'same-origin');
  }
});

test('closing the form while math verification is pending prevents the slider from reopening', async () => {
  let resolve;
  const pending = new Promise(r => { resolve = r; });
  const h = harness(path => path === '/api/auth/math/verify' ? pending : null);
  await h.open();
  h.$('#authMathAnswer').value = '7';
  const submission = h.$('#authNext').click();
  h.context.auth.closeAuth();
  resolve(reply({math_token: 'late-proof'}));
  await submission;
  assert.equal(h.calls.filter(c => c.path === '/api/auth/captcha').length, 0);
  assert.equal(h.$('#authPw').value, '');
});

test('changing mode discards an earlier question response and sets the password autocomplete', async () => {
  let resolve, count = 0;
  const pending = new Promise(r => { resolve = r; });
  const h = harness(path => path === '/api/auth/math' && ++count === 1 ? pending : null);
  h.context.auth.openAuth('login');
  h.context.auth.setMode('register');
  await settle();
  resolve(reply({cid: 'stale', question: '9 + 9 = ?'}));
  await settle();
  assert.equal(h.$('#authMathQuestion').textContent, '3 + 4 = ?');
  assert.equal(h.$('#authPw').autocomplete, 'new-password');
});

test('expired math authorization returns to a usable form instead of displaying a broken slider', async () => {
  const h = harness(path => path === '/api/auth/captcha' ? reply({error: '请先回答算术题'}, 403) : null);
  await h.open();
  h.$('#authMathAnswer').value = '7';
  await h.$('#authNext').click();
  await settle();
  assert.equal(h.$('#authStep2').style.display, 'none');
  assert.equal(h.$('#authNext').disabled, false);
  assert.equal(h.$('#authErr').textContent, '请先回答算术题');
});

test('incorrect credentials return to the form so the password can be corrected', async () => {
  const h = harness(path => path === '/api/auth/login' ? reply({error: '邮箱或密码错误'}, 403) : null);
  await h.open();
  h.$('#authMathAnswer').value = '7';
  await h.$('#authNext').click();
  await h.context.auth.verifyCaptcha();
  await settle();
  assert.equal(h.$('#authStep2').style.display, 'none');
  assert.equal(h.$('#authErr').textContent, '邮箱或密码错误');
  assert.equal(h.$('#authNext').disabled, false);
});

test('failed logout reports an error without pretending the user signed out', async () => {
  const h = harness(path => path === '/api/auth/logout' ? reply({error: 'unavailable'}, 503) : null);
  await h.$('#logoutBtn').click();
  assert.equal(h.$('#error').textContent, '退出失败，请重试');
  assert.equal(h.calls.some(c => c.path === '/api/auth/me'), false);
});
