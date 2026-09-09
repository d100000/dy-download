const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');

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

function createHarness(fetchImpl, file = files[0], lang = 'zh') {
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
    LANG: lang,
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
      if (message.startsWith('下载已开始') || message.startsWith('Download started.')) successes.push(message);
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
    clearTimeout() {},
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
    videoDirectDownloadURL: context.videoDirectDownloadURL,
    videoDownloadRefreshURL: context.videoDownloadRefreshURL,
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

test('parser video without a direct address retains its signed fallback', async () => {
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

test('share downloads without a direct address retain the one-byte signed preflight', async () => {
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

test('batch single and download-all forward primary addresses when only top-level source is present', async () => {
  const direct = 'https://v26-default.365yg.com/video/original/';
  const h = createHarness(async url => {
    assert.equal(url, direct);
    return response(200, 'original-video');
  });
  const btn = {innerHTML: '下载全部', textContent: '下载全部', disabled: false};
  const context = {
    _batch: [{kind: 'video', source: 'parser', video: {direct_url: direct, filename: '作品.mp4'}}],
    $: () => btn,
    browserDownload: h.browserDownload,
    videoDirectDownloadURL: h.videoDirectDownloadURL,
    videoDownloadRefreshURL: h.videoDownloadRefreshURL,
    setTimeout(fn) { fn(); },
  };
  vm.runInNewContext(homepage.slice(homepage.indexOf('async function dlBatchOne('),
    homepage.indexOf('async function exportBatch(')), context);
  await context.dlBatchOne(0, {innerHTML: '下载', textContent: '下载', disabled: false});
  await context.downloadAllVideos();
  assert.equal(h.fetches.length, 2);
  assert.equal(h.clicks.length, 2);
  assert.equal(h.objectUrls.length, 2);
  assert.deepEqual(h.errors, []);
  assert.equal(btn.textContent, '下载全部');
});

for (const file of files.slice(0, 2)) {
  const direct = 'https://v26-default.365yg.com/video/original/';
  const fallback = '/api/media/video/item_bytecdn?exp=123&sig=abc';
  const button = () => ({innerHTML: '下载', textContent: '下载', disabled: false});

  test(`${file}: an expired address shows loading, polls regeneration and saves the new original`, async () => {
    const refresh = '/api/media/video/item_bytecdn/download-link?exp=123&sig=abc';
    const fresh = 'https://v26-default.365yg.com/video/refreshed/';
    const video = {source: 'parser', direct_url: direct, url: direct,
      download_refresh_url: refresh, filename: '作品.mp4', width: 1080};
    const btn = button();
    let polls = 0;
    const h = createHarness(async (url, options) => {
      if (url === direct) return response(403, 'expired', 'text/html');
      if (url === refresh) {
        assert.equal(btn.disabled, true);
        assert.match(btn.textContent, /正在重新获取下载链接/);
        assert.equal(options.method, 'POST');
        assert.equal(options.mode, 'same-origin');
        assert.deepEqual(JSON.parse(options.body), {failed_url: direct});
        return ++polls === 1
          ? response(202, JSON.stringify({status: 'processing', retry_after_ms: 2000}), 'application/json')
          : response(200, JSON.stringify({status: 'ready', url: fresh}), 'application/json');
      }
      assert.equal(url, fresh, 'must not request the server media fallback');
      return response(200, 'refreshed-complete-video');
    }, file);
    assert.equal(await h.browserDownload(direct, '作品.mp4', btn, fallback, true, direct, video), true);
    assert.equal(polls, 2);
    assert.equal(video.direct_url, fresh);
    assert.equal(video.filename, '作品.mp4');
    assert.equal(video.width, 1080);
    assert.equal(await h.objectUrls[0].text(), 'refreshed-complete-video');
    assert.deepEqual(h.errors, []);
    assert.equal(btn.disabled, false);
    assert.equal(btn.innerHTML, '下载');
    // 同一结果再次下载直接使用新地址，不重复创建任务。
    await h.browserDownload(video.url, video.filename, btn, fallback, true,
      h.videoDirectDownloadURL(video), video);
    assert.equal(polls, 2);
  });

  test(`${file}: a share with no cached address can refresh and download on click`, async () => {
    const refresh = '/api/media/video/item_bytecdn/download-link?exp=123&sig=abc';
    const video = {source: 'parser', download_refresh_url: refresh};
    const h = createHarness(async url => url === refresh
      ? response(200, JSON.stringify({status: 'ready', url: direct}), 'application/json')
      : response(200, 'complete-video'), file);
    assert.equal(await h.browserDownload('', '作品.mp4', button(), '', true, '', video), true);
    assert.deepEqual(h.fetches.map(r=>r.url), [refresh, direct]);
    assert.equal(h.clicks.length, 1);
  });

  test(`${file}: refresh failure is bounded, neutral and restores the button`, async () => {
    const refresh = '/api/media/video/item_bytecdn/download-link?exp=123&sig=abc';
    for (const payload of [response(502, JSON.stringify({error: 'AnyToCopy secret=x'}), 'application/json'),
        response(200, JSON.stringify({status: 'ready', url: 'javascript:alert(1)'}), 'application/json'),
        response(202, JSON.stringify({status: 'processing'}), 'application/json')]) {
      const btn = button();
      const h = createHarness(async () => payload, file);
      assert.equal(await h.browserDownload('', '作品.mp4', btn, '', true, '',
        {source: 'parser', download_refresh_url: refresh}), false);
      assert.ok(h.fetches.length <= 155);
      assert.equal(h.clicks.length, 0);
      assert.equal(h.successes.length, 0);
      assert.equal(btn.disabled, false);
      assert.equal(btn.innerHTML, '下载');
      assert.doesNotMatch(h.errors.join(' '), /AnyToCopy|secret/);
    }
    const h = createHarness(async () => { throw new Error('must not fetch a remote refresh endpoint'); }, file);
    assert.equal(h.videoDownloadRefreshURL({download_refresh_url: 'https://evil.example/api/media/video/item_bytecdn/download-link'}), '');
  });

  test(`${file}: primary address downloads complete bytes without calling the blocked server`, async () => {
    const h = createHarness(async url => {
      assert.equal(url, direct, 'must not request the server media route');
      return response(200, 'complete-original-video');
    }, file);
    const btn = button();
    const selected = h.videoDirectDownloadURL({source: 'parser', direct_url: direct});
    assert.equal(await h.browserDownload('', '作品.mp4', btn, fallback, true, selected), true);
    assert.equal(h.fetches.length, 1);
    assert.equal(h.fetches[0].options.mode, 'cors');
    assert.equal(h.fetches[0].options.credentials, 'omit');
    assert.equal(await h.objectUrls[0].text(), 'complete-original-video');
    assert.equal(h.clicks[0].href, 'blob:test-download');
    assert.equal(h.clicks[0].download, '作品.mp4');
    assert.equal(h.clicks[0].target, undefined);
    assert.deepEqual(h.errors, []);
    assert.equal(btn.innerHTML, '下载');
    assert.equal(btn.disabled, false);
  });

  test(`${file}: a primary address remains downloadable without any signed fallback`, async () => {
    const h = createHarness(async () => response(200, 'original-video'), file);
    assert.equal(await h.browserDownload('', '作品.mp4', button(), '', true, direct), true);
    assert.equal(h.clicks.length, 1);
    assert.equal(h.fetches.length, 1);
  });

  test(`${file}: CORS failure tries the same-origin fallback after the primary address`, async () => {
    const h = createHarness(async url => {
      if (url === direct) throw new TypeError('Failed to fetch');
      return response(206, 'x');
    }, file);
    assert.equal(await h.browserDownload('', '作品.mp4', button(), fallback, true, direct), true);
    assert.equal(h.fetches.length, 2);
    assert.equal(h.fetches[0].url, direct);
    assert.equal(h.fetches[1].options.headers.Range, 'bytes=0-0');
    assert.equal(h.clicks[0].target, 'nativeDownloadTarget');
    assert.equal(h.objectUrls.length, 0);
  });

  test(`${file}: failed direct and proxy routes show a neutral retry message`, async () => {
    const h = createHarness(async url => url === direct
      ? response(403, 'expired', 'text/html')
      : response(503, JSON.stringify({error: '内部供应商与代理配置详情'}), 'application/json'), file);
    const btn = button();
    assert.equal(await h.browserDownload('', '作品.mp4', btn, fallback, true, direct), false);
    assert.equal(h.clicks.length, 0);
    assert.equal(h.successes.length, 0);
    assert.deepEqual(h.errors, ['原片下载暂时不可用，请重新解析后再试']);
    assert.equal(btn.disabled, false);
  });

  test(`${file}: invalid, partial and truncated responses are never saved as video`, async () => {
    const truncated = response(200, 'x');
    const get = truncated.headers.get;
    truncated.headers.get = name => name.toLowerCase() === 'content-length' ? '100' : get(name);
    for (const resp of [response(200, '<html>error</html>', 'text/html'),
        response(200, '{}', 'application/json'), response(200, 'image', 'image/jpeg'),
        response(206, 'x'), response(200, ''), truncated]) {
      const h = createHarness(async () => resp, file);
      assert.equal(await h.browserDownload('', '作品.mp4', button(), '', true, direct), false);
      assert.equal(h.clicks.length, 0);
      assert.equal(h.objectUrls.length, 0);
      assert.equal(h.successes.length, 0);
    }
  });

  test(`${file}: provider selection supports old responses without using official URLs as primary`, () => {
    const select = createHarness(async () => {}, file).videoDirectDownloadURL;
    assert.equal(select({source: 'parser', direct_url: direct}), direct);
    assert.equal(select({url: direct}, 'parser'), direct);
    assert.equal(select({source: 'atc', url: direct}), direct);
    assert.equal(select({source: 'douyin', atc_url: direct}), direct);
    assert.equal(select({source: 'douyin_direct', direct_url: direct}), '');
    for (const value of ['javascript:alert(1)', 'http://example.com/v',
        'https://user:password@example.com/v', 'https://example.com:8443/v', '/api/media/video/test']) {
      assert.equal(select({source: 'parser', direct_url: value}), '');
    }
  });
}

for (const file of ['static/index.html', 'static/share.html']) {
  test(`${file}: English TikTok refresh loading, success and errors use the selected language`, async () => {
    const old = 'https://v16-webapp-prime.tiktok.com/expired.mp4';
    const fresh = 'https://v16-webapp-prime.tiktok.com/fresh.mp4';
    const refresh = '/api/media/video/tiktok_6718335390845095173/download-link?exp=1&sig=test';
    const btn = {innerHTML:'Download',textContent:'Download',disabled:false,setAttribute(){},removeAttribute(){}};
    const states = [];
    const h = createHarness(async url => {
      states.push(btn.textContent);
      if (url === old) return response(403, 'expired', 'text/plain');
      if (url === refresh) return response(200, JSON.stringify({status: 'ready', url: fresh}), 'application/json');
      assert.equal(url, fresh);
      return response(200, 'video-bytes');
    }, file, 'en');
    const video = {source: 'parser', direct_url: old, download_refresh_url: refresh};
    assert.equal(await h.browserDownload('', '原始标题.mp4', btn, '', true, old, video), true);
    assert.ok(states.includes('Refreshing download link…'));
    assert.equal(h.clicks[0].download, '原始标题.mp4');
    assert.match(h.successes[0], /^Download started\./);
    assert.equal(btn.disabled, false);
    const failed = createHarness(async () => response(502, '{}', 'application/json'), file, 'en');
    assert.equal(await failed.browserDownload('', 'original.mp4', btn, '', true, '', video), false);
    assert.match(failed.errors[0], /Could not refresh/);
    assert.doesNotMatch(failed.errors[0], /[\u4e00-\u9fff]|anytocopy/i);
  });
}
