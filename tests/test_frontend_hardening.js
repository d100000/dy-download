const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const homepage = fs.readFileSync('static/index.html', 'utf8');
const share = fs.readFileSync('static/share.html', 'utf8');
const oss = fs.readFileSync('oss/static/index.html', 'utf8');

test('provider strings never enter inline JavaScript arguments', () => {
  for (const html of [homepage, share, oss]) {
    assert.doesNotMatch(html, /onclick="[^"]*(?:browserDownload|saveImage|dl)\('\$\{/);
  }
  assert.match(homepage, /onclick="downloadAlbumImage\(this\)"/);
  assert.match(share, /onclick="saveImageAt\(\$\{i\}\)"/);
  assert.match(oss, /onclick="downloadImageAt\(\$\{i\},this\)"/);
});

test('homepage playback can fall back from the WeChat proxy to direct media', () => {
  const start = homepage.indexOf('function videoPlaySrc(v){');
  const end = homepage.indexOf('function videoHTML(d){', start);
  assert.ok(start >= 0 && end > start, 'homepage playback helpers not found');
  const context = {
    IS_WECHAT_UA: true,
    esc(value) { return String(value || ''); },
    markVideoMetadataUnavailable() {
      throw new Error('fallback ended before trying direct media');
    },
  };
  vm.runInNewContext(homepage.slice(start, end) + `
    this.videoPlaySrc = videoPlaySrc;
    this.videoProxyFallback = videoProxyFallback;
  `, context, {filename: 'static/index.html'});
  const video = {
    dataset: {
      direct: 'https://v3.douyinvod.com/direct.mp4',
      atc: '', alt: 'https://v6.douyinvod.com/alt.mp4',
      proxy: '/api/douyin/video/signed', source: 'douyin_direct',
      playIndex: '0',
    },
    currentTime: 0,
    src: '/api/douyin/video/signed',
    load() {},
    play() {},
  };
  assert.equal(context.videoPlaySrc({
    source: 'douyin_direct', proxy_url: video.dataset.proxy,
    url: video.dataset.direct, alt_url: video.dataset.alt,
  }), video.dataset.proxy);
  context.videoProxyFallback(video);
  assert.equal(video.src, video.dataset.direct);
  assert.equal(video.dataset.playIndex, '1');
});

test('clipboard write has an explicit legacy fallback', async () => {
  const start = homepage.indexOf('async function copyText(text, fallbackEl=null){');
  const end = homepage.indexOf('const fmtDur =', start);
  assert.ok(start >= 0 && end > start, 'copy helper not found');
  let copied = false;
  let removed = false;
  const area = {
    value: '', style: {}, setAttribute() {}, select() {},
    remove() { removed = true; },
  };
  const context = {
    navigator: {},
    document: {
      body: {appendChild(node) { assert.equal(node, area); }},
      createElement(name) { assert.equal(name, 'textarea'); return area; },
      execCommand(command) { copied = command === 'copy'; return copied; },
    },
  };
  vm.runInNewContext(homepage.slice(start, end) + '\nthis.copyText = copyText;', context);
  assert.equal(await context.copyText('safe text'), true);
  assert.equal(area.value, 'safe text');
  assert.equal(copied, true);
  assert.equal(removed, true);
  assert.doesNotMatch(homepage + share, /navigator\.clipboard\?\.writeText\([^\n]+\)\.then/);
});

test('gallery failures stay on the page and OSS WeChat uses same-origin media', () => {
  const start = share.indexOf('function imgFail(image){');
  const end = share.indexOf('/* ---------------- \u5206\u4eab\u6d77\u62a5', start);
  assert.ok(start >= 0 && end > start, 'share image failure helper not found');
  assert.doesNotMatch(share.slice(start, end), /location\.reload/);
  assert.match(oss, /if\(IS_WECHAT_UA&&v\.proxy_url\)return v\.proxy_url/);
  assert.match(homepage + share + oss, /download_url\|\|[^\n]*proxy_url\|\|[^\n]*url/);
});
