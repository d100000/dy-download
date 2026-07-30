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

function createHarness(fetchImpl) {
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
  vm.runInNewContext(source, context, {filename: 'static/index.html'});
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

test('expired payload without a signed endpoint fails before network access', async () => {
  const harness = createHarness(async () => {
    throw new Error('fetch must not be called');
  });

  const ok = await harness.browserDownload(
    'https://aweme.snssdk.com/aweme/v1/play/?video_id=private',
    '作品.mp4',
    null,
    '',
    true);

  assert.equal(ok, false);
  assert.equal(harness.fetches.length, 0);
  assert.equal(harness.clicks.length, 0);
  assert.equal(harness.opened.length, 0);
  assert.match(harness.errors[0], /重新解析/);
});
