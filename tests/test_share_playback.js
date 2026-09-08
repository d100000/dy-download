const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('static/share.html', 'utf8');
const start = html.indexOf('let _playSession = 0;');
const end = html.indexOf('/* ---------------- 下载 ---------------- */', start);
assert.ok(start >= 0 && end > start, 'share playback state machine not found');
const playbackSource = html.slice(start, end) + '\nthis.startPlay = startPlay;';

function pendingPlayResult() {
  return {
    then() { return {catch() {}}; },
    catch() {},
  };
}

function resolvedPlayResult() {
  return {
    then(fn) { fn(); return {catch() {}}; },
    catch() {},
  };
}

class FakeVideo {
  constructor(playResult) {
    this.listeners = new Map();
    this.dataset = {};
    this.style = {setProperty() {}};
    this.currentTime = 0;
    this.paused = true;
    this.readyState = 0;
    this.networkState = 2;
    this.error = null;
    this.src = '';
    this.videoWidth = 0;
    this.videoHeight = 0;
    this.playResult = playResult || pendingPlayResult();
  }

  setAttribute() {}
  load() {}
  play() { return this.playResult; }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) {
    this.listeners.get(name)?.delete(fn);
  }
  dispatch(name) {
    for (const fn of [...(this.listeners.get(name) || [])]) fn();
  }
}

function createHarness(options = {}) {
  const timers = [];
  const videos = [];
  const tracks = [];
  const toasts = [];
  const masks = [];
  const elements = {
    playerBox: {
      children: [],
      style: {values: {}, setProperty(name, value) { this.values[name] = value; }},
      classList: {toggle() {}},
      appendChild(node) {
        this.children.push(node);
        if (node.id) elements[node.id] = node;
      },
    },
    playBtn: {style: {}},
    loading: {style: {}, textContent: ''},
    poster: {style: {}},
  };

  const context = {
    S: {
      cover: '',
      data: {
        video: {
          url: 'https://dy1.example/video',
          alt_url: 'https://dy2.example/video',
          proxy_url: '/api/video/signed',
        },
      },
    },
    IS_WECHAT_UA: true,
    document: {
      getElementById(id) { return elements[id] || null; },
      createElement(name) {
        assert.equal(name, 'video');
        const video = new FakeVideo(options.playResult);
        videos.push(video);
        return video;
      },
    },
    track(kind, data) { tracks.push({kind, data}); },
    toast(message) { toasts.push(message); },
    showMask(...args) { masks.push(args); },
    setTimeout(fn, delay) {
      const timer = {fn, delay, cancelled: false};
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      if (timer) timer.cancelled = true;
    },
    Date,
    window: {innerWidth: 390, innerHeight: 844},
  };
  if (options.video) context.S.data.video = options.video;
  if (options.priority) context.S.data.play_priority = options.priority;
  vm.runInNewContext(playbackSource, context, {filename: 'static/share.html'});

  function runNextTimer() {
    const timer = timers.find(item => !item.cancelled && !item.ran);
    if (!timer) return false;
    timer.ran = true;
    timer.fn();
    return true;
  }

  return {
    startPlay: context.startPlay,
    elements,
    videos,
    tracks,
    toasts,
    masks,
    timers,
    runAllTimers() {
      let count = 0;
      while (runNextTimer()) {
        if (++count > 20) throw new Error('timer loop did not settle');
      }
    },
  };
}

test('timeupdate confirms playback and cancels the failure path', () => {
  const h = createHarness();
  h.startPlay();
  const video = h.videos[0];
  video.currentTime = 0.25;
  video.paused = false;
  video.dispatch('timeupdate');
  h.runAllTimers();

  assert.equal(h.tracks.filter(item => item.kind === 'play_ok').length, 1);
  assert.equal(h.masks.length, 0);
  assert.equal(h.toasts.length, 0);
});

test('a stale route timeout cannot fail the newer route', () => {
  const h = createHarness();
  h.startPlay();
  const video = h.videos[0];
  const staleTimeout = h.timers[0].fn;

  // 微信内也是 dy1 → dy2 → proxy：首条 dy1 失败后切到 dy2
  video.dispatch('error');
  assert.equal(video.src, 'https://dy2.example/video');
  staleTimeout();
  assert.equal(video.src, 'https://dy2.example/video');
});

test('repeated start reuses one player and automatic failure never opens a mask', () => {
  const h = createHarness();
  h.startPlay();
  h.startPlay();
  assert.equal(h.videos.length, 1);
  assert.equal(h.elements.playerBox.children.length, 1);

  h.runAllTimers();
  assert.equal(h.masks.length, 0);
  assert.equal(h.toasts.length, 1);
  assert.match(h.toasts[0], /画面已播放可继续观看/);
});

test('loaded metadata changes the player box to the real landscape ratio', () => {
  const h = createHarness();
  h.startPlay();
  const video = h.videos[0];
  video.videoWidth = 1920;
  video.videoHeight = 1080;
  video.dispatch('loadedmetadata');
  assert.equal(h.elements.playerBox.style.values['--video-ratio'], '1920 / 1080');
  assert.match(h.elements.playerBox.style.values['--player-width'], /px$/);
});

test('a resolved play promise alone does not report playback success', () => {
  const h = createHarness({playResult: resolvedPlayResult()});
  h.startPlay();
  const video = h.videos[0];
  assert.equal(h.tracks.filter(item => item.kind === 'play_ok').length, 0);
  video.currentTime = 0.1;
  video.paused = false;
  video.readyState = 2;
  video.dispatch('playing');
  assert.equal(h.tracks.filter(item => item.kind === 'play_ok').length, 1);
});

test('players use metadata-driven aspect ratios and stay centered', () => {
  const portrait = html.match(/\.player\{([^}]*)\}/)?.[1] || '';
  const landscape = html.match(/\.player\.wide\{([^}]*)\}/)?.[1] || '';

  assert.match(portrait, /width:min\(100%,var\(--player-width,39\.375vh\)\)/);
  assert.match(portrait, /aspect-ratio:var\(--video-ratio,9\/16\)/);
  assert.match(portrait, /margin-inline:auto/);
  assert.match(landscape, /aspect-ratio:var\(--video-ratio,16\/9\)/);
  assert.match(html, /function syncPlayerRatio/);
  assert.match(html, /loadedmetadata/);
  assert.match(html, /syncPlayerRatio\(v\)/);
  assert.doesNotMatch(portrait, /aspect-ratio:9\/16/);
});

test('share page hides interaction counts and backend provider details', () => {
  assert.doesNotMatch(html, /function statsBlock/);
  assert.doesNotMatch(html, /class="stats"/);
  assert.doesNotMatch(html, /class="stat"/);
  assert.doesNotMatch(html, /\[['"]点赞['"],/);
  assert.doesNotMatch(html, /❤.*💬.*★/);
  assert.doesNotMatch(html, /AnyToCopy/i);
  assert.doesNotMatch(html, /直连优先 · 兼容线路兜底/);
  assert.match(html, /第三方内容解析服务/);
});


test('neutral parser source honors direct-media priority', () => {
  const h = createHarness({video: {
    source: 'parser', url: 'https://v3.douyinvod.com/main.mp4',
    direct_url: 'https://v3.douyinvod.com/main.mp4',
    proxy_url: '/api/media/video/item_123?exp=1&sig=test',
  }, priority: ['atc', 'proxy', 'dy1', 'dy2']});
  h.startPlay();
  assert.equal(h.videos[0].src, 'https://v3.douyinvod.com/main.mp4');
});
