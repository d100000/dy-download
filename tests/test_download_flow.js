const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const files = [
  'static/index.html',
  'static/share.html',
  'oss/static/index.html',
];

const homepage = fs.readFileSync(files[0], 'utf8');
const start = homepage.indexOf('function downloadTarget');
const end = homepage.indexOf('function downloadVideoItem', start);
assert.ok(start >= 0 && end > start, 'homepage download implementation not found');
const source = homepage.slice(start, end)
  + '\nthis.browserDownload = browserDownload;';

function response(status, body, contentType = 'video/mp4') {
  const blob = new Blob([body], {type: contentType});
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        const key = name.toLowerCase();
        if (key === 'content-type') return contentType;
        if (key === 'content-length') return String(blob.size);
        if (key === 'content-range' && status === 206) return 'bytes 0-0/100';
        return null;
      },
    },
    async blob() {
      return blob;
    },
    async arrayBuffer() {
      return blob.arrayBuffer();
    },
    async json() {
      return JSON.parse(String(body));
    },
  };
}

function createHarness(fetchImpl, file = files[0]) {
  const fetches = [];
  const clicks = [];
  const errors = [];
  const successes = [];
  const opened = [];
  const elements = {};
  const objectUrls = [];

  class HarnessURL extends URL {}
  HarnessURL.createObjectURL = blob => {
    objectUrls.push(blob);
    return 'blob:test-download';
  };
  HarnessURL.revokeObjectURL = () => {};

  const context = {
    AbortController,
    Blob,
    URL: HarnessURL,
    location: {
      href: 'https://downloader.example/',
      origin: 'https://downloader.example',
    },
    document: {
      body: {
        appendChild(node) {
          if (node.id) elements[node.id] = node;
        },
      },
      getElementById(id) {
        return elements[id] || null;
      },
      createElement(name) {
        if (name === 'iframe') {
          return {
            hidden: false,
            setAttribute() {},
          };
        }
        assert.equal(name, 'a');
        return {
          style: {},
          click() {
            clicks.push({
              href: this.href,
              download: this.download,
              target: this.target,
            });
          },
          remove() {},
        };
      },
    },
    async fetch(url, options) {
      fetches.push({url, options});
      return fetchImpl(url, options, fetches.length);
    },
    showError(message) {
      errors.push(message);
    },
    showOk(message) {
      successes.push(message);
    },
    toast(message) {
      if (message.startsWith('下载已开始')) successes.push(message);
      else errors.push(message);
    },
    window: {
      open(...args) {
        opened.push(args);
      },
    },
    setTimeout(fn) {
      fn();
      return 1;
    },
  };
  let js = source;
  if (file === 'static/share.html') {
    const share = fs.readFileSync(file, 'utf8');
    js = share.slice(share.indexOf('function downloadTarget'), share.indexOf('function imgFail'))
      + '\nthis.browserDownload = browserDownload;';
  }
  vm.runInNewContext(js, context, {filename: file});
  return {
    browserDownload: context.browserDownload,
    fetches,
    clicks,
    errors,
    successes,
    opened,
    objectUrls,
  };
}

test('all frontends remove navigation-to-Douyin download fallbacks', () => {
  for (const file of files) {
    const html = fs.readFileSync(file, 'utf8');
    assert.doesNotMatch(html, /window\.open\s*\(/, file);
    assert.match(html, /preflightVideoDownload/, file);
    assert.match(html, /bytes=0-0/, file);
    assert.match(html, /nativeDownloadTarget/, file);
  }
  assert.match(
    fs.readFileSync('static/share.html', 'utf8'),
    /onclick="download\(this\)"/);
  assert.match(
    fs.readFileSync('oss/static/index.html', 'utf8'),
    /download_url\|\|_video\.proxy_url,true/);
});

test('video download fetches only the signed same-origin endpoint', async () => {
  const harness = createHarness(async () => response(206, 'x'));
  const button = {innerHTML: '<span>下载</span>', textContent: '下载', disabled: false};

  const ok = await harness.browserDownload(
    'https://aweme.snssdk.com/aweme/v1/play/?video_id=private',
    '作品.mp4',
    button,
    '/api/video/video_id_12345?exp=123&sig=abc',
    true);

  assert.equal(ok, true, JSON.stringify(harness.errors));
  assert.equal(harness.fetches.length, 1);
  assert.match(harness.fetches[0].url, /^\/api\/video\/video_id_12345\?/);
  assert.doesNotMatch(harness.fetches[0].url, /aweme\.snssdk\.com/);
  assert.equal(harness.fetches[0].options.mode, 'same-origin');
  assert.equal(harness.fetches[0].options.headers.Range, 'bytes=0-0');
  assert.equal(harness.clicks.length, 1);
  assert.match(harness.clicks[0].href, /^\/api\/video\/video_id_12345\?/);
  assert.equal(harness.clicks[0].target, 'nativeDownloadTarget');
  assert.equal(harness.clicks[0].download, undefined);
  assert.equal(harness.objectUrls.length, 0);
  assert.equal(harness.errors.length, 0);
  assert.equal(harness.opened.length, 0);
  assert.equal(button.innerHTML, '<span>下载</span>');
  assert.equal(button.disabled, false);
});

test('parser video download accepts only its signed same-origin endpoint', async () => {
  const harness = createHarness(async () => response(206, 'x'));
  const ok = await harness.browserDownload(
    'https://v3.douyinvod.com/video.mp4',
    '作品.mp4',
    null,
    '/api/media/video/item_1234567890abcdef?exp=123&sig=abc',
    true);

  assert.equal(ok, true, JSON.stringify(harness.errors));
  assert.equal(harness.fetches.length, 1);
  assert.match(harness.fetches[0].url, /^\/api\/media\/video\/item_1234567890abcdef\?/);
  assert.equal(harness.fetches[0].options.mode, 'same-origin');
  assert.equal(harness.clicks.length, 1);
  assert.equal(harness.clicks[0].target, 'nativeDownloadTarget');
  assert.equal(harness.objectUrls.length, 0);
});

test('502 stays in-page, retries once, and never clicks or navigates', async () => {
  const harness = createHarness(async () => response(
    502,
    JSON.stringify({error: '视频下载线路暂时不可用，请稍后重试'}),
    'application/json'));
  const button = {innerHTML: '下载', textContent: '下载', disabled: false};

  const ok = await harness.browserDownload(
    'https://aweme.snssdk.com/aweme/v1/play/?video_id=private',
    '作品.mp4',
    button,
    '/api/video/video_id_12345?exp=123&sig=abc',
    true);

  assert.equal(ok, false);
  assert.equal(harness.fetches.length, 2);
  assert.equal(harness.clicks.length, 0);
  assert.equal(harness.opened.length, 0);
  assert.deepEqual(
    harness.errors,
    ['视频下载线路暂时不可用，请稍后重试']);
  assert.equal(button.disabled, false);
});

test('missing signed download routes are not reported as expired links', async () => {
  for (const file of files.slice(0, 2)) {
    const harness = createHarness(async () => {
      throw new Error('fetch must not be called');
    }, file);

    const ok = await harness.browserDownload(
      'https://aweme.snssdk.com/aweme/v1/play/?video_id=private',
      '作品.mp4',
      {innerHTML: '下载', disabled: false},
      '',
      true);

    assert.equal(ok, false);
    assert.equal(harness.fetches.length, 0);
    assert.equal(harness.clicks.length, 0);
    assert.equal(harness.opened.length, 0);
    assert.match(harness.errors[0], /重新解析/);
    assert.match(harness.errors[0], /下载线路/);
    assert.doesNotMatch(harness.errors[0], /已过期/);
  }
});

test('share CDN downloads use the signed same-origin route and a one-byte preflight', async () => {
  const harness = createHarness(async () => response(206, 'x'), 'static/share.html');
  const ok = await harness.browserDownload('https://v26-default.365yg.com/video/test/',
    '视频.mp4', {innerHTML: '下载', disabled: false},
    '/api/media/video/item_bytecdn?exp=123&sig=abc', true);
  assert.equal(ok, true);
  assert.equal(harness.fetches.length, 1);
  assert.equal(harness.fetches[0].options.headers.Range, 'bytes=0-0');
  assert.match(harness.clicks[0].href, /^\/api\/media\/video\//);
  assert.equal(harness.clicks[0].target, 'nativeDownloadTarget');
  assert.equal(harness.errors.length, 0);
});
