const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');

const homepage = fs.readFileSync('static/index.html', 'utf8');
const start = homepage.indexOf('const fmtDur =');
const end = homepage.indexOf('function extrasBlock', start);
assert.ok(start >= 0 && end > start, 'homepage video metadata helpers not found');

const source = homepage.slice(start, end) + `
this.fmtDur = fmtDur;
this.videoDurationMs = videoDurationMs;
this.videoDurationText = videoDurationText;
this.videoResolutionText = videoResolutionText;
this.engagementHTML = engagementHTML;
`;
const context = {LANG: 'zh'};
vm.runInNewContext(source, context, {filename: 'static/index.html'});

function createMetadataHarness() {
  const functionStart = homepage.indexOf('const _albums =');
  const functionEnd = homepage.indexOf('function render(d){', functionStart);
  assert.ok(functionStart >= 0 && functionEnd > functionStart,
    'homepage metadata event wiring not found');

  const listeners = new Map();
  const pendingClasses = new Set(['is-pending']);
  const duration = {
    textContent: '读取中…',
    classList: {remove(name) { pendingClasses.delete(name); }, contains(name) { return pendingClasses.has(name); }},
  };
  const resolution = {
    textContent: '读取中…',
    classList: {remove() {}, contains(name) { return name === 'is-pending'; }},
  };
  const scope = {
    querySelectorAll(selector) {
      if (selector === '[data-video-duration]') return [duration];
      if (selector === '[data-video-resolution]') return [resolution];
      if (selector === '[data-video-duration],[data-video-resolution]') return [duration, resolution];
      return [];
    },
  };
  const video = {
    duration: 125.4,
    videoWidth: 1080,
    videoHeight: 1920,
    readyState: 0,
    dataset: {itemId: 'item_test'},
    closest() { return scope; },
    addEventListener(name, fn) { listeners.set(name, fn); },
  };
  const result = {querySelector() { return null; }};
  const eventContext = {
    result,
    _batch: [],
    fmtDur: context.fmtDur,
    videoDurationMs: context.videoDurationMs,
  };
  const eventSource = homepage.slice(functionStart, functionEnd) + `
this.wireVideoMetadata = wireVideoMetadata;
this.videos = _videos;
`;
  vm.runInNewContext(eventSource, eventContext, {filename: 'static/index.html'});
  eventContext.videos.item_test = {};
  eventContext.wireVideoMetadata({querySelectorAll() { return [video]; }});
  return {eventContext, listeners, video, duration, resolution, pendingClasses};
}

test('engagement shows only available counts and preserves zero', () => {
  const html = context.engagementHTML({digg: 93774, comment: 0, share: null, collect: '6792'});
  assert.match(html, /<dt>点赞<\/dt><dd>93,774<\/dd>/);
  assert.match(html, /<dt>评论<\/dt><dd>0<\/dd>/);
  assert.match(html, /<dt>收藏<\/dt><dd>6,792<\/dd>/);
  assert.doesNotMatch(html, /<dt>分享<\/dt>/);
});

test('missing or invalid counts never produce values or an empty row', () => {
  for (const stats of [undefined, null, {}, {digg: null, comment: '', share: '  ', collect: false}]) {
    assert.equal(context.engagementHTML(stats), '');
  }
  for (const digg of [true, false, [], {}, -1, 1.5, Infinity, NaN, 'unknown', '1.2万', '<img onerror=alert(1)>']) {
    assert.equal(context.engagementHTML({digg}), '');
  }
});

test('English engagement labels preserve full count precision', () => {
  const english = {LANG: 'en'};
  vm.runInNewContext(source, english);
  const html = english.engagementHTML({digg: 123456789, comment: '0', share: 12, collect: 34});
  assert.match(html, /<dt>Likes<\/dt><dd>123,456,789<\/dd>/);
  assert.match(html, /<dt>Comments<\/dt><dd>0<\/dd>/);
  assert.match(html, /<dt>Shares<\/dt><dd>12<\/dd>/);
  assert.match(html, /<dt>Saves<\/dt><dd>34<\/dd>/);
});

test('video and gallery cards conditionally render engagement without empty space', () => {
  const renderStart = homepage.indexOf('function videoHTML(d){');
  const renderEnd = homepage.indexOf('async function downloadAll(', renderStart);
  assert.ok(renderStart >= 0 && renderEnd > renderStart);
  const renderContext = {
    URL,
    LANG: 'zh', esc: String, platformName: () => '抖音', sharePageSupported: () => false,
    videoPlaySrc: () => '', videoDatasetHTML: () => '', authorRow: () => '', extrasBlock: () => '',
  };
  const directStart = homepage.indexOf('function videoDirectDownloadURL(');
  const directEnd = homepage.indexOf('function downloadFallbackURL(', directStart);
  vm.runInNewContext(source + homepage.slice(directStart, directEnd)
    + homepage.slice(renderStart, renderEnd), renderContext);
  for (const render of [renderContext.videoHTML, renderContext.albumHTML]) {
    const item = {item_id: 'test', title: '测试作品', video: {}, images: []};
    assert.doesNotMatch(render(item), /class="engagement-row"/);
    assert.match(render({...item, stats: {comment: 0}}), /<dt>评论<\/dt><dd>0<\/dd>/);
    assert.doesNotMatch(render({...item, stats: {digg: null, comment: ''}}), /class="engagement-row"/);
  }
  assert.match(renderContext.videoHTML({item_id: 'test', title: '测试作品', source: 'parser',
    video: {direct_url: 'https://v26-default.365yg.com/video/original/'}}), /onclick="downloadVideoItem\(this\)"/);
});

test('video elements wire native metadata events', () => {
  assert.match(homepage, /data-read-metadata="1"/);
  assert.match(homepage, /addEventListener\('loadedmetadata'/);
  assert.match(homepage, /addEventListener\('durationchange'/);
  assert.match(homepage, /video\.videoWidth/);
  assert.match(homepage, /video\.videoHeight/);
});

test('loadedmetadata fills visible values and the in-memory result', () => {
  const h = createMetadataHarness();
  assert.equal(typeof h.listeners.get('loadedmetadata'), 'function');
  h.listeners.get('loadedmetadata')();
  assert.equal(h.duration.textContent, '2:05');
  assert.equal(h.resolution.textContent, '1080×1920');
  assert.equal(h.pendingClasses.has('is-pending'), false);
  assert.equal(h.eventContext.videos.item_test.duration_ms, 125400);
  assert.equal(h.eventContext.videos.item_test.width, 1080);
  assert.equal(h.eventContext.videos.item_test.height, 1920);
});

test('duration helpers use real media seconds and support hour-long videos', () => {
  assert.equal(context.videoDurationMs({duration: 125.4}), 125400);
  assert.equal(context.videoDurationMs({duration: Infinity}), 0);
  assert.equal(context.videoDurationMs({duration: Number.NaN}), 0);
  assert.equal(context.videoDurationMs({duration: 0}), 0);
  assert.equal(context.fmtDur(3723000), '1:02:03');
  assert.equal(context.videoDurationText(0), '读取中…');
  assert.equal(context.videoDurationText(Infinity), '读取中…');
});

test('resolution helper never invents a 720P fallback', () => {
  assert.equal(
    context.videoResolutionText({width: '1920', height: '1080'}),
    '1920×1080');
  assert.equal(context.videoResolutionText({}), '读取中…');
  assert.equal(
    context.videoResolutionText({width: Infinity, height: 1080}),
    '读取中…');
  assert.doesNotMatch(homepage, />720P</);
});

test('missing homepage titles and captions omit the module; real captions remain escaped', () => {
  const ctx = {esc: value => String(value).replace(/</g, '&lt;').replace(/>/g, '&gt;')};
  vm.runInNewContext(homepage.slice(homepage.indexOf('function resultCaption'), homepage.indexOf('function extrasBlock')), ctx);
  for (const title of [undefined, null, '', ' ', '暂无标题', '(无标题)', '（无标题）', 'Untitled']) {
    assert.equal(ctx.resultTitleHTML({title}), '');
  }
  assert.equal(ctx.resultTitleHTML({title:'暂无标题', content:'真实正文'}), '<div class="v-title">真实正文</div>');
  assert.equal(ctx.resultTitleHTML({title:'<img>'}), '<div class="v-title">&lt;img&gt;</div>');
});
