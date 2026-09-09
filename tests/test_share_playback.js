const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');

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
  remove() {}
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
    context,
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
  assert.match(h.elements.playerBox.style.values['--player-width'], /vh$/);
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

test('share page hides backend provider details', () => {
  assert.doesNotMatch(html, /AnyToCopy/i);
  assert.doesNotMatch(html, /直连优先 · 兼容线路兜底/);
  assert.match(html, /第三方内容解析服务/);
});

function metadataHarness(data = {}, share = {}) {
  const context = {
    S: {kind: 'video', title: '分享标题', author: '作者', item_id: '7682023366300556537', data, ...share},
    URL,
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    fmtDate: () => '2026-09-09',
    fmtTimestamp: ts => ts ? '2026-09-09 12:30' : '',
  };
  const start = html.indexOf('function publicLink');
  const end = html.indexOf('function render(){', start);
  vm.runInNewContext(html.slice(start, end), context);
  return context;
}

test('share snapshot counts preserve zero and hide all missing or invalid values', () => {
  const c = metadataHarness();
  const content = c.engagementBlock({stats: {digg: 93774, comment: 0, share: null, collect: '6792'}});
  assert.match(content, /<dt>点赞<\/dt><dd>93,774<\/dd>/);
  assert.match(content, /<dt>评论<\/dt><dd>0<\/dd>/);
  assert.match(content, /<dt>收藏<\/dt><dd>6,792<\/dd>/);
  assert.doesNotMatch(content, /<dt>分享/);
  for (const value of [undefined, null, '', ' ', false, -1, 1.2, NaN, Infinity, {}, [], '1万', '<img>']) {
    assert.equal(c.engagementBlock({stats: {digg: value}}), '');
  }
});

test('share metadata only renders available video fields and supports albums', () => {
  const c = metadataHarness();
  assert.equal(c.metadataBlock({}), '');
  const content = c.metadataBlock({duration_ms: 3672000, video: {width: 1920, height: 1080, filename: 'test.mp4'}});
  assert.match(content, /1:01:12/);
  assert.match(content, /1920 × 1080/);
  assert.match(content, /MP4/);
  c.S.kind = 'note';
  assert.equal(c.metadataBlock({duration_ms: 1000, video: {width: 1920, height: 1080}}), '');
});

test('author profile uses saved fields, escapes text and rejects unsafe profile links', () => {
  const c = metadataHarness();
  assert.equal(c.profileBlock({}), '');
  const profile = c.profileBlock({author_detail: {follower_count: 0, total_favorited: 620201, signature: '<script>evil</script>'}});
  assert.match(profile, /<dt>粉丝<\/dt><dd>0<\/dd>/);
  assert.match(profile, /620,201/);
  assert.match(profile, /&lt;script&gt;/);
  assert.doesNotMatch(profile, /<script>/);
  assert.doesNotMatch(c.authorBlock({author_url: 'javascript:alert(1)'}), /href=/);
  assert.doesNotMatch(c.authorBlock({author_url: 'https://user:secret@example.com'}), /href=/);
  assert.match(c.authorBlock({author_url: 'https://www.douyin.com/user/abc'}), /作者主页/);
});

test('custom share titles keep the full original caption and tags available', () => {
  const c = metadataHarness();
  const title = '完整文案'.repeat(200) + '<img>';
  assert.match(c.captionBlock({title}), /原作品文案/);
  assert.ok(c.captionBlock({title}).includes('完整文案'.repeat(200)));
  assert.doesNotMatch(c.captionBlock({title}), /<img>/);
  const tags = Array.from({length: 12}, (_, i) => 'tag' + i);
  assert.match(c.extraBlock({tags}), /#tag11/);
});

test('ready shares read metadata snapshots and separately check media availability', () => {
  const start = html.indexOf('/* ---------------- 入口 ---------------- */');
  for (const available of [true, false]) {
    const calls = [];
    vm.runInNewContext(html.slice(start, html.indexOf('</script>', start)), {
      S: {state: 'ok', kind: 'video', media_available: available},
      render: () => calls.push('render'), setupWxShare: () => calls.push('wx'),
      prepareShareMedia: () => calls.push('media'),
      scheduleStatusPoll: () => { throw Error('ready shares must not poll'); },
    });
    assert.deepEqual(calls, ['render', 'wx', 'media']);
  }
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

test('English TikTok share counts and metadata preserve zero and original captions', () => {
  const c = metadataHarness({}, {item_id:'tiktok_6718335390845095173'});
  c.LANG = 'en';
  const counts = c.engagementBlock({stats: {digg: 1234, comment: 0}});
  assert.match(counts, /<dt>Likes<\/dt><dd>1,234/);
  assert.match(counts, /<dt>Comments<\/dt><dd>0/);
  assert.doesNotMatch(counts, /<dt>Shares|<dt>Saves/);
  assert.match(c.metadataBlock({duration_ms: 12000}), /Duration/);
  assert.match(c.authorBlock({platform: 'tiktok'}), /TikTok · Video creator/);
  const original_url = 'https://www.tiktok.com/@creator/video/6718335390845095173';
  const caption = c.captionBlock({title:'原始中文标题 #标签', original_url});
  assert.ok(caption.includes('原始中文标题 #标签'));
  assert.ok(caption.includes(original_url));
  assert.doesNotMatch(caption, /douyin.com/);
});

test('localized template labels preserve nested elements and user content', () => {
  const c = {};
  vm.runInNewContext('', c);
  c.LANG = 'en';
  const text = {nodeType: 3, textContent: '下载'};
  const icon = {nodeType: 1, textContent: 'icon'};
  const label = {children:[icon], childNodes:[text,icon], getAttribute:()=> '下载'};
  c.localizeUi({querySelectorAll:()=>[label]});
  assert.equal(text.textContent, 'Download');
  assert.equal(icon.textContent, 'icon');
});

test('share playback displays actual file dimensions and duration without changing its saved snapshot', () => {
  const data = {duration_ms: 10000, video: {width: 2160, height: 3840, filename: 'original.mp4'},
    stats: {digg: 33796, comment: 161}, title: '替你自由'};
  const original = JSON.stringify(data);
  const c = metadataHarness(data);
  const target = {innerHTML: ''};
  c.document = {getElementById: id => id === 'mediaMetadata' ? target : null};
  vm.runInNewContext(playbackSource, c);
  c.syncPlayerMetadata({videoWidth: 1080, videoHeight: 1920, duration: 9.5});
  assert.match(target.innerHTML, /1080 × 1920/);
  assert.match(target.innerHTML, /0:10/);
  assert.doesNotMatch(target.innerHTML, /2160|3840/);
  assert.equal(JSON.stringify(data), original);
  const previous = target.innerHTML;
  c.syncPlayerMetadata({videoWidth: 0, videoHeight: 0, duration: NaN});
  assert.equal(target.innerHTML, previous);
  c.LANG = 'en';
  c.syncPlayerMetadata({videoWidth: 720, videoHeight: 1280, duration: 9.5});
  assert.match(target.innerHTML, /Resolution/);
  assert.match(target.innerHTML, /720 × 1280/);
  assert.equal(JSON.stringify(data), original);
});


test('opening a healthy share checks metadata without refreshing or autoplaying', async () => {
  const h = createHarness({video:{source:'parser', url:'https://v3.douyinvod.com/good.mp4', download_refresh_url:'/signed'}});
  h.context.S.state = 'ok'; h.context.S.kind = 'video';
  let refreshes = 0;
  h.context.refreshVideoDownloadLink = async () => { refreshes++; throw Error('unexpected'); };
  FakeVideo.prototype.removeAttribute = function() {};
  const prepared = h.context.prepareShareMedia();
  assert.equal(h.videos.length, 1);
  assert.equal(h.videos[0].preload, 'metadata');
  h.videos[0].dispatch('loadedmetadata');
  await prepared;
  assert.equal(refreshes, 0);
  assert.equal(h.elements.player, undefined);
});

test('opening an expired share and concurrent checks renew once and retain metadata', async () => {
  const video = {source:'parser', url:'https://v3.douyinvod.com/expired.mp4', download_refresh_url:'/signed'};
  const h = createHarness({video});
  h.context.S.state = 'ok'; h.context.S.kind = 'video';
  h.context.S.title = 'saved'; h.context.S.data.stats = {digg:0,comment:12};
  let refreshes = 0;
  h.context.refreshVideoDownloadLink = async (model) => { refreshes++; model.url='https://v3.douyinvod.com/fresh.mp4'; return model.url; };
  const first = h.context.prepareShareMedia();
  const second = h.context.prepareShareMedia();
  assert.equal(first, second);
  h.videos[0].dispatch('error');
  await first;
  assert.equal(refreshes, 1);
  assert.equal(video.url, 'https://v3.douyinvod.com/fresh.mp4');
  assert.equal(h.context.S.title, 'saved');
  assert.deepEqual(h.context.S.data.stats, {digg:0,comment:12});
  assert.equal(h.elements.player, undefined);
});

test('missing address on open refreshes once without a media probe', async () => {
  const video = {source:'parser',proxy_url:'/proxy',download_refresh_url:'/signed'};
  const h = createHarness({video});
  Object.assign(h.context.S,{state:'ok',kind:'video'});
  let refreshes = 0;
  h.context.refreshVideoDownloadLink = async model => { refreshes++; model.url='https://v3.douyinvod.com/new.mp4'; return model.url; };
  await h.context.prepareShareMedia();
  assert.equal(refreshes,1);
  assert.equal(h.videos.length,0);
});

test('playback renews failed primary once then can use the fallback', async () => {
  const h = createHarness({video:{source:'parser',url:'https://v3.douyinvod.com/old.mp4',proxy_url:'/proxy',download_refresh_url:'/signed'}});
  let refreshes = 0;
  h.context.refreshVideoDownloadLink = async () => { refreshes++; return 'https://v3.douyinvod.com/new.mp4'; };
  h.startPlay();
  const player = h.videos[0]; player.dispatch('error');
  await new Promise(setImmediate);
  assert.equal(refreshes,1);
  assert.equal(player.src,'https://v3.douyinvod.com/new.mp4');
  player.dispatch('error');
  assert.equal(player.src,'/proxy');
  player.currentTime = 1; player.dispatch('timeupdate');
  h.runAllTimers();
  assert.equal(refreshes,1);
  assert.equal(h.toasts.length,0);
});

test('renewal failure stays bounded and does not prevent backup playback', async () => {
  const h = createHarness({video:{source:'parser',url:'https://v3.douyinvod.com/old.mp4',proxy_url:'/proxy',download_refresh_url:'/signed'}});
  let refreshes=0;
  h.context.refreshVideoDownloadLink=async()=>{refreshes++;throw Error('unavailable');};
  h.startPlay(); h.videos[0].dispatch('error');
  await new Promise(setImmediate);
  assert.equal(h.videos[0].src,'/proxy');
  h.videos[0].dispatch('error'); h.runAllTimers();
  assert.equal(refreshes,1);
  assert.equal(h.elements.playBtn.style.display,'');
});
