const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const homepage = fs.readFileSync('static/index.html', 'utf8');
const start = homepage.indexOf('const fmtDur =');
const end = homepage.indexOf('function extrasBlock', start);
assert.ok(start >= 0 && end > start, 'homepage video metadata helpers not found');

const source = homepage.slice(start, end) + `
this.fmtDur = fmtDur;
this.videoDurationMs = videoDurationMs;
this.videoDurationText = videoDurationText;
this.videoResolutionText = videoResolutionText;
`;
const context = {};
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

test('result UI no longer renders interaction-count cards', () => {
  assert.doesNotMatch(homepage, /function statsRow/);
  assert.doesNotMatch(homepage, /class="stats-row"/);
  assert.doesNotMatch(homepage, /class="stat-chip"/);
  assert.doesNotMatch(homepage, /<th>互动数据<\/th>/);
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
