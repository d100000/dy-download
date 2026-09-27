const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');
const homepage = fs.readFileSync('static/index.html', 'utf8');
const share = fs.readFileSync('static/share.html', 'utf8');
const slice = (source, start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `missing script boundary: ${start}`);
  return source.slice(a, b);
};

function timers(){
  let now=0, seq=0;
  const jobs=new Map();
  class TestDate extends Date { static now(){return now;} }
  return {
    jobs, Date:TestDate, advance(ms){now+=ms;},
    setTimeout(fn,ms){const id=++seq;jobs.set(id,{fn,at:now+ms});return id;},
    clearTimeout(id){jobs.delete(id);},
    async next(){
      const next=[...jobs].sort((a,b)=>a[1].at-b[1].at)[0];
      if(!next)return false;
      jobs.delete(next[0]);now=Math.max(now,next[1].at);await next[1].fn();return true;
    },
  };
}

function item(id='7689777123456789012',kind='video'){
  return {item_id:id,kind,title:'片段…',title_source:'share_text',title_status:'partial',metadata_status:'pending',
    metadata_url:'/api/parse-metadata/'+id+'?kind='+kind+'&exp=100&sig=test',
    video:{url:'https://media.test/playing.mp4',filename:'片段.mp4',currentTime:14},
    images:kind==='note'?[{url:'https://media.test/one.jpg',filename:'old_01.jpg'}]:undefined};
}
function fresh(data, overrides={}){
  return {item_id:data.item_id,kind:data.kind,title:'完整作品标题',title_source:'browser_structured',title_status:'complete',metadata_status:'ready',
    video:{filename:'完整作品标题.mp4',url:'https://media.test/must-not-replace.mp4'},
    images:[{index:1,filename:'完整作品标题_01.jpg',url:'https://media.test/must-not-replace.jpg'}],...overrides};
}
const escapeHTML=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function metadataFresh(data, overrides={}){
  return fresh(data,{content:'完整作品正文',content_status:'available',author:'真实作者 <tag>',
    avatar:'https://p3.douyinpic.com/avatar.jpg',author_url:'https://www.douyin.com/user/public-author',
    create_time:1790404437,duration_ms:125400,snapshot_at:1790404500,
    stats:{digg:0,comment:12,share:101,collect:0},tags:['真实话题','<script>'],
    metadata_source:'browser_structured',metadata_fields:['author','stats.digg','duration_ms'],
    video:{...fresh(data).video,width:1920,height:1080,proxy_url:'/must-not-replace'},...overrides});
}
function htmlSlot(){
  let html='';
  return {style:{},hidden:false,textContent:'',writes:0,classList:{remove(){}},
    get innerHTML(){return html;},set innerHTML(value){html=value;this.writes++;}};
}
function harness(items=[item()]){
  const clock=timers(), calls=[];
  const nodes=items.map(data=>({dataset:{titleItem:data.item_id},style:{},textContent:data.title}));
  const copyNodes=items.map(data=>({dataset:{titleCopy:data.item_id},style:{display:'none'}}));
  const models=Object.fromEntries(items.map(data=>[data.item_id,{...data.video}]));
  const players=[];
  const metadataNodes=items.map(data=>{
    const slots=Object.fromEntries(['author','stats','extras','published','date','duration','resolution'].map(key=>[key,htmlSlot()]));
    slots.published.style.display='none';
    slots.published.querySelector=selector=>selector==='[data-metadata-date]'?slots.date:null;
    const select=selector=>{
      const key=selector.match(/^\[data-(?:metadata|video)-([a-z]+)\]$/)?.[1];
      return slots[key]||null;
    };
    const scope={querySelectorAll:selector=>select(selector)?[select(selector)]:[],querySelector:()=>player,
      set innerHTML(_value){throw Error('metadata must not rebuild the card');}};
    const player={currentTime:14,src:'https://media.test/playing.mp4',paused:false,videoWidth:0,videoHeight:0,duration:NaN,
      dataset:{itemId:data.item_id},load(){throw Error('metadata must not load media');},play(){throw Error('metadata must not restart playback');},
      closest:selector=>selector==='.card,.pm-body'?scope:null};
    players.push(player);
    return {dataset:{metadataItem:data.item_id},slots,querySelector:select,querySelectorAll:scope.querySelectorAll,
      closest:()=>scope,set innerHTML(_value){throw Error('metadata must not rebuild info');}};
  });
  const context={...clock,URL,AbortController,parseRun:1,input:{value:'the current input'},_batch:[],
    esc:escapeHTML,platformName:()=> '抖音',wireAvatar(){},
    location:{origin:'https://local.test'},_videos:models,
    document:{hidden:false,querySelectorAll:selector=>({'[data-title-item]':nodes,'[data-title-copy]':copyNodes,
      '[data-metadata-item]':metadataNodes}[selector]||[])},
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({ok:true,data:fresh(items.find(d=>url.includes(d.item_id)))})};},
  };
  vm.runInNewContext(slice(homepage,'const fmtDur =', 'const _albums =')
    +slice(homepage,'function syncVideoAspectRatio(', 'function render(d)')
    +slice(homepage,'function authorRow(', '/* ---- 作者悬停浮层')
    +slice(homepage,'const TITLE_POLL_WINDOW_MS', '/* ================= 文案提取'),context);
  return {context,clock,calls,nodes,copyNodes,models,items,metadataNodes,players};
}

test('single-result metadata updates title and filename without replacing playback state',async()=>{
  const h=harness(),data=h.items[0],video=data.video,model=h.models[data.item_id];
  h.context.startResultTitlePolling(h.items);
  assert.equal(h.calls.length,0);
  await h.clock.next();
  assert.equal(h.calls.length,1);
  assert.match(h.calls[0].url,/^https:\/\/local\.test\/api\/parse-metadata\//);
  assert.equal(h.calls[0].options.credentials,'same-origin');
  assert.equal(h.calls[0].options.cache,'no-store');
  assert.equal(data.title,'完整作品标题');
  assert.equal(data.video,video);assert.equal(data.video.currentTime,14);
  assert.equal(data.video.url,'https://media.test/playing.mp4');
  assert.equal(model.filename,'完整作品标题.mp4');
  assert.equal(h.nodes[0].textContent,'完整作品标题');
  assert.equal(h.copyNodes[0].style.display,'');
  assert.equal(h.clock.jobs.size,0);
});

test('homepage metadata fills real author, date, stats and technical slots while retaining media objects',()=>{
  const h=harness(),data=h.items[0],video=data.video,model=h.models[data.item_id],player=h.players[0];
  const slots=h.metadataNodes[0].slots;
  assert.equal(h.context.applyResultTitleMetadata(data,metadataFresh(data)),true);
  assert.equal(data.video,video);assert.equal(h.models[data.item_id],model);
  assert.equal(data.video.url,'https://media.test/playing.mp4');assert.equal(model.url,data.video.url);
  assert.equal(data.video.proxy_url,undefined);assert.equal(player.currentTime,14);assert.equal(player.paused,false);
  assert.equal(player.src,'https://media.test/playing.mp4');
  assert.match(slots.author.innerHTML,/真实作者 &lt;tag&gt;/);
  assert.match(slots.author.innerHTML,/https:\/\/www\.douyin\.com\/user\/public-author/);
  assert.match(slots.stats.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.match(slots.stats.innerHTML,/<dt>收藏<\/dt><dd>0<\/dd>/);
  assert.equal(slots.published.style.display,'');assert.match(slots.date.textContent,/2026-09-26/);
  assert.equal(slots.duration.textContent,'2:05');assert.equal(slots.resolution.textContent,'1920×1080');
  assert.match(slots.extras.innerHTML,/#真实话题/);assert.match(slots.extras.innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(slots.extras.innerHTML,/<script>/);
  assert.equal(data.content,'完整作品正文');assert.equal(data.snapshot_at,1790404500);
  assert.equal(data.metadata_source,'browser_structured');
});

test('metadata-only responses update missing homepage fields without replacing local title or filenames',()=>{
  const h=harness(),data=h.items[0],slots=h.metadataNodes[0].slots;
  h.context.applyResultTitleMetadata(data,metadataFresh(data,{title:'（无标题）',metadata_status:'pending'}));
  assert.equal(data.title,'片段…');assert.equal(data.video.filename,'片段.mp4');
  assert.equal(data.author,'真实作者 <tag>');assert.match(slots.stats.innerHTML,/<dd>0<\/dd>/);
  assert.equal(slots.duration.textContent,'2:05');
});

test('homepage poll does not replace measured playback dimensions with snapshot dimensions',()=>{
  const h=harness(),data=h.items[0],player=h.players[0],slots=h.metadataNodes[0].slots;
  Object.assign(player,{videoWidth:720,videoHeight:1280,duration:9.5});
  h.context.applyResultTitleMetadata(data,metadataFresh(data));
  assert.equal(slots.resolution.textContent,'720×1280');assert.equal(slots.duration.textContent,'0:10');
  assert.equal(data.video.width,1920);assert.equal(data.video.height,1080);
  assert.equal(h.models[data.item_id].width,720);assert.equal(h.models[data.item_id].height,1280);
  assert.equal(player.currentTime,14);assert.equal(player.paused,false);
});

test('homepage metadata ignores invalid counts and unrelated identities before touching any slot',()=>{
  const h=harness(),data=h.items[0],slots=h.metadataNodes[0].slots;
  for(const identity of [{item_id:'another'},{kind:'note'}]){
    assert.equal(h.context.applyResultTitleMetadata(data,metadataFresh(data,identity)),false);
    assert.equal(data.author,undefined);assert.equal(data.stats,undefined);assert.equal(slots.author.writes,0);
    assert.equal(slots.stats.writes,0);assert.equal(slots.published.style.display,'none');
  }
  data.stats={digg:0,comment:4};
  h.context.applyResultTitleMetadata(data,metadataFresh(data,{stats:{digg:null,comment:false,share:-1,collect:'12'}}));
  assert.deepEqual(data.stats,{digg:0,comment:4});
  assert.match(slots.stats.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.doesNotMatch(slots.stats.innerHTML,/<dt>分享|<dt>收藏/);
});

test('unchanged homepage author metadata does not rebuild its node or attach duplicate hover handlers',()=>{
  const h=harness(),data=h.items[0],author=h.metadataNodes[0].slots.author;
  let bindings=0;h.context.wireAvatar=()=>{bindings++;};
  h.context.applyResultTitleMetadata(data,metadataFresh(data));
  h.context.applyResultTitleMetadata(data,metadataFresh(data));
  assert.equal(author.writes,1);assert.equal(bindings,1);
  h.context.applyResultTitleMetadata(data,metadataFresh(data,{author:'补齐的作者名'}));
  assert.equal(author.writes,2);assert.equal(bindings,2);
});

test('empty homepage metadata placeholders do not add flex gaps while remaining available for later updates',()=>{
  for(const slot of ['author','stats','extras']){
    assert.match(homepage,new RegExp('\\[data-metadata-'+slot+'\\]:empty'));
    assert.match(homepage,new RegExp('<div data-metadata-'+slot+'>'));
  }
  assert.match(homepage,/\[data-metadata-author\]:empty,\[data-metadata-stats\]:empty,\[data-metadata-extras\]:empty\{display:none\}/);
  assert.match(share,/<div id="shareEngagement">\$\{engagementBlock\(d\)\}<\/div>/);
  assert.match(share,/<div id="mediaMetadata">\$\{metadataBlock\(d\)\}<\/div>/);
});

test('batch rows and an open gallery retain their objects and media while titles arrive independently',async()=>{
  const first=item('1111111111111111111'),second=item('2222222222222222222','note');
  const h=harness([first,second]),images=second.images,original=images[0];
  const popup={dataset:{titleItem:second.item_id},style:{},textContent:'片段…'};
  h.nodes.push(popup);
  h.context.startResultTitlePolling(h.items);
  await h.clock.next();await h.clock.next();
  assert.equal(first.title,'完整作品标题');assert.equal(second.title,'完整作品标题');
  assert.equal(second.images,images);assert.equal(images[0],original);
  assert.equal(images[0].url,'https://media.test/one.jpg');
  assert.equal(images[0].filename,'完整作品标题_01.jpg');
  assert.equal(popup.textContent,'完整作品标题');
  assert.match(homepage,/startResultTitlePolling\(_batch\)/);
  assert.match(homepage,/startResultTitlePolling\(\[d\]\)/);
});

test('existing batch rows expose metadata slots and refresh their compact author, counts and duration',()=>{
  const h=harness(),data=h.items[0],node=h.metadataNodes[0],slots=node.slots;
  node.dataset.metadataView='batch';
  h.context._batch=h.items;h.context.result={innerHTML:''};h.context.$=()=>({});
  Object.assign(h.context,{sharePageSupported:()=>false,videoDownloadRefreshURL:()=>'',videoDirectDownloadURL:()=>'',
    albumImageSrc:image=>image?.url||'',revealRenderedResult(){},maybeRegTip(){},exportBatch(){},downloadAllVideos(){}});
  vm.runInNewContext(slice(homepage,'function renderBatch(', 'async function dlBatchOne('),h.context);
  h.context.renderBatch(1,[]);
  assert.match(h.context.result.innerHTML,new RegExp('<tr data-metadata-item="'+data.item_id+'" data-metadata-view="batch">'));
  assert.match(h.context.result.innerHTML,/class="bt-a" data-metadata-author/);
  assert.match(h.context.result.innerHTML,/<div data-metadata-stats>/);
  assert.match(h.context.result.innerHTML,/data-batch-duration="0" data-video-duration/);
  h.context.applyResultTitleMetadata(data,metadataFresh(data));
  assert.equal(slots.author.innerHTML,'抖音 · @真实作者 &lt;tag&gt;');
  assert.doesNotMatch(slots.author.innerHTML,/author-row|<img/);
  assert.match(slots.stats.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.equal(slots.duration.textContent,'2:05');
  h.context.stopResultTitlePolling();
});

test('already-open previews refresh metadata slots without replacing the player or gallery scroll state',()=>{
  for(const kind of ['video','note']){
    const h=harness([item('3333333333333333333',kind)]),data=h.items[0];
    const stats=htmlSlot(),duration=htmlSlot(),resolution=htmlSlot(),preview=htmlSlot();
    const player=h.players[0],images=data.images,image=images?.[0];
    Object.assign(preview,{dataset:{},scrollTop:137,
      closest:()=>preview,
      querySelector:selector=>({'[data-metadata-stats]':stats,'[data-video-duration]':duration,
        '[data-video-resolution]':resolution,'video[data-read-metadata]':kind==='video'?player:null}[selector]||null),
      querySelectorAll:selector=>({'[data-video-duration]':[duration],'[data-video-resolution]':[resolution]}[selector]||[])});
    const modal={querySelector:()=>preview,classList:{add(){}}};
    Object.assign(h.context,{_batch:h.items,$:()=>modal,albumImageSrc:im=>im.url,
      videoDownloadRefreshURL:()=>'',videoDirectDownloadURL:()=>'',videoPlaySrc:v=>v.url,videoDatasetHTML:()=>'',
      wireVideoMetadata(){},wireVideoPlayback(){}});
    h.context.document.body={style:{}};
    vm.runInNewContext(slice(homepage,'function openPlayer(', 'function closePlayer('),h.context);
    h.context.openPlayer(0);
    assert.equal(preview.dataset.metadataItem,data.item_id);assert.equal(preview.dataset.metadataView,'preview');
    assert.match(preview.innerHTML,/<div data-metadata-stats>/);
    const html=preview.innerHTML,writes=preview.writes;
    h.metadataNodes.push(preview);
    h.context.applyResultTitleMetadata(data,metadataFresh(data));
    assert.match(stats.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
    assert.equal(preview.writes,writes);assert.equal(preview.innerHTML,html);assert.equal(preview.scrollTop,137);
    assert.equal(player.src,'https://media.test/playing.mp4');assert.equal(player.currentTime,14);assert.equal(player.paused,false);
    if(kind==='note'){
      assert.equal(data.images,images);assert.equal(images[0],image);assert.equal(image.url,'https://media.test/one.jpg');
    }else{
      assert.equal(duration.textContent,'2:05');assert.equal(resolution.textContent,'1920×1080');
    }
  }
});

test('a missing snapshot title keeps request-local hints and their filenames while pending',async()=>{
  const h=harness(),data=h.items[0];
  h.context.fetch=async()=>({ok:true,json:async()=>({ok:true,data:fresh(data,{title:'（无标题）',metadata_status:'pending'})})});
  h.context.startResultTitlePolling(h.items);await h.clock.next();
  assert.equal(data.title,'片段…');assert.equal(data.title_source,'share_text');
  assert.equal(data.video.filename,'片段.mp4');
  assert.equal(h.clock.jobs.size,1);
  h.context.stopResultTitlePolling();assert.equal(h.clock.jobs.size,0);
});

test('wrong identities cannot mutate the displayed title or download filenames',()=>{
  const h=harness(),data=h.items[0];
  for(const changes of [{item_id:'another'},{kind:'note'}]){
    assert.equal(h.context.applyResultTitleMetadata(data,fresh(data,changes)),false);
    assert.equal(data.title,'片段…');assert.equal(data.video.filename,'片段.mp4');
  }
});

test('new render aborts a pending request and rejects its eventual stale response',async()=>{
  const h=harness(),old=h.items[0];let resolve,signal;
  h.context.fetch=async(_url,options)=>{signal=options.signal;return new Promise(r=>{resolve=r;});};
  h.context.startResultTitlePolling(h.items);
  const pending=h.clock.next();
  h.context.startResultTitlePolling([item('3333333333333333333')]);
  assert.equal(signal.aborted,true);
  resolve({ok:true,json:async()=>({ok:true,data:fresh(old)})});await pending;
  assert.equal(old.title,'片段…');assert.equal(h.nodes[0].textContent,'片段…');
  h.context.stopResultTitlePolling();
});

test('edited inputs and new parse generations discard in-flight results',async()=>{
  for(const change of [h=>{h.context.input.value='different input';},h=>{h.context.parseRun++;}]){
    const h=harness(),data=h.items[0];let resolve;
    h.context.fetch=async()=>new Promise(r=>{resolve=r;});
    h.context.startResultTitlePolling(h.items);const pending=h.clock.next();change(h);
    resolve({ok:true,json:async()=>({ok:true,data:fresh(data)})});await pending;
    assert.equal(data.title,'片段…');assert.equal(h.clock.jobs.size,0);
  }
});

test('polling refuses external or unrelated URLs and stops at its bounded deadline',async()=>{
  for(const url of ['https://evil.test/api/parse-metadata/id','//evil.test/api/parse-metadata/id','/api/parse','javascript:alert(1)']){
    const h=harness();h.items[0].metadata_url=url;h.context.startResultTitlePolling(h.items);
    assert.equal(h.clock.jobs.size,0);
  }
  const h=harness();h.context.document.hidden=true;h.context.startResultTitlePolling(h.items);
  h.clock.advance(11000);await h.clock.next();
  assert.equal(h.calls.length,0);assert.equal(h.clock.jobs.size,0);
});

test('terminal endpoint failures stop polling; temporary failures can retry',async()=>{
  for(const status of [403,404,500]){
    const h=harness();h.context.fetch=async()=>({ok:false,status});
    h.context.startResultTitlePolling(h.items);await h.clock.next();
    assert.equal(h.clock.jobs.size,status===500?1:0);
    assert.equal(h.items[0].title,'片段…');h.context.stopResultTitlePolling();
  }
});

function shareHarness(data=item()){
  const clock=timers(),intervals=[];let calls=0;
  const nodes=Object.fromEntries(['shareTitle','shareTitleHint','shareEngagement','mediaMetadata','snapshotNote','playerBox'].map(id=>[id,htmlSlot()]));
  nodes.playerBox.style.setProperty=()=>{};nodes.playerBox.classList={toggle(){}};
  const player={src:'https://media.test/playing.mp4',currentTime:14,paused:false,videoWidth:0,videoHeight:0,duration:NaN,
    load(){throw Error('share metadata must not load media');},play(){throw Error('share metadata must not restart playback');}};
  const context={...clock,URL,AbortController,S:{sid:'test-share',item_id:data.item_id,kind:data.kind,state:'ok',title:data.title,data},
    esc:escapeHTML,fmtTimestamp:ts=>ts?'timestamp '+ts:'',
    document:{hidden:false,getElementById:id=>nodes[id]||null,querySelector:selector=>selector==='#playerBox video'?player:null},updateWxShare(){},
    render(){throw Error('metadata must not rebuild the share page');},
    setInterval(_fn,ms){intervals.push(ms);},
    fetch:async()=>{calls++;return {ok:true,json:async()=>({sid:'test-share',item_id:data.item_id,kind:data.kind,state:'ok',title:'完整作品标题',data:fresh(data)})};},
  };
  vm.runInNewContext(slice(share,'function shareTitle(){','function shareReferralURL(')
    +slice(share,'function syncPlayerRatio(', '// 打开时只加载媒体元数据'),context);
  return {context,clock,intervals,nodes,player,data,get calls(){return calls;}};
}

test('share pages briefly poll pending titles, then retain the normal administrator-title refresh interval',async()=>{
  const h=shareHarness();h.context.startTitleRefresh();
  assert.deepEqual(h.intervals,[30000]);assert.equal(h.clock.jobs.size,1);
  await h.clock.next();await Promise.resolve();await Promise.resolve();
  assert.equal(h.calls,1);assert.equal(h.context.S.data.metadata_status,'ready');
  assert.equal(h.context.S.title,'完整作品标题');assert.equal(h.clock.jobs.size,0);
});

test('share quick polling remains bounded and avoids requests from hidden pages',async()=>{
  const h=shareHarness();h.context.startTitleRefresh();h.context.document.hidden=true;
  await h.clock.next();assert.equal(h.calls,0);assert.equal(h.clock.jobs.size,0);
  h.context.document.hidden=false;h.clock.advance(11000);h.context.scheduleQuickTitleRefresh();
  assert.equal(h.clock.jobs.size,0);
});

test('share refresh fills engagement and metadata wrappers with zero counts without changing playback',async()=>{
  const h=shareHarness(),data=h.data,video=data.video,player=h.player;
  const next={sid:'test-share',item_id:data.item_id,kind:data.kind,state:'ok',title:'完整作品标题',data:metadataFresh(data)};
  h.context.fetch=async()=>({ok:true,json:async()=>next});
  await h.context.refreshShareTitle();
  assert.equal(h.context.S.data,data);assert.equal(data.video,video);
  assert.equal(data.video.url,'https://media.test/playing.mp4');assert.equal(data.video.proxy_url,undefined);
  assert.equal(data.video.filename,'完整作品标题.mp4');
  assert.equal(player.src,'https://media.test/playing.mp4');assert.equal(player.currentTime,14);assert.equal(player.paused,false);
  assert.match(h.nodes.shareEngagement.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.match(h.nodes.shareEngagement.innerHTML,/<dt>收藏<\/dt><dd>0<\/dd>/);
  assert.match(h.nodes.mediaMetadata.innerHTML,/2:05/);assert.match(h.nodes.mediaMetadata.innerHTML,/1920 × 1080/);
  assert.match(h.nodes.mediaMetadata.innerHTML,/timestamp 1790404437/);
  assert.equal(h.nodes.snapshotNote.hidden,false);assert.match(h.nodes.snapshotNote.textContent,/1790404500/);
  assert.equal(h.nodes.shareTitle.textContent,'完整作品标题');assert.equal(data.author,'真实作者 <tag>');
});

test('share metadata refresh preserves measured file dimensions and duration',async()=>{
  const h=shareHarness();Object.assign(h.player,{videoWidth:720,videoHeight:1280,duration:9.5});
  h.context.fetch=async()=>({ok:true,json:async()=>({sid:'test-share',item_id:h.data.item_id,kind:h.data.kind,state:'ok',
    title:'完整作品标题',data:metadataFresh(h.data)})});
  await h.context.refreshShareTitle();
  assert.match(h.nodes.mediaMetadata.innerHTML,/720 × 1280/);assert.match(h.nodes.mediaMetadata.innerHTML,/0:10/);
  assert.doesNotMatch(h.nodes.mediaMetadata.innerHTML,/1920 × 1080|2:05/);
  assert.equal(h.data.video.width,1920);assert.equal(h.data.video.height,1080);assert.equal(h.data.duration_ms,125400);
  assert.equal(h.player.currentTime,14);assert.equal(h.player.paused,false);
});

test('share rejects outer and nested identity mismatches before updating title, stats or media metadata',async()=>{
  for(const changes of [{sid:'other'},{item_id:'other'},{kind:'note'},
    {data:{item_id:'other',kind:'video'}},{data:{kind:'note'}}]){
    const h=shareHarness(),before=JSON.stringify(h.context.S);
    const next={sid:'test-share',item_id:h.data.item_id,kind:h.data.kind,state:'ok',title:'不应显示',data:metadataFresh(h.data),...changes};
    h.context.fetch=async()=>({ok:true,json:async()=>next});
    await h.context.refreshShareTitle();
    assert.equal(JSON.stringify(h.context.S),before);
    assert.equal(h.nodes.shareEngagement.writes,0);assert.equal(h.nodes.mediaMetadata.writes,0);
    assert.equal(h.nodes.shareTitle.textContent,'');assert.equal(h.player.currentTime,14);
  }
});

test('share refresh rejects invalid counts and keeps existing zero values and gallery image objects',async()=>{
  const data=item('2222222222222222222','note');data.stats={digg:0,comment:4};data.images[0].index=1;
  const h=shareHarness(data),images=data.images,image=images[0];
  h.context.fetch=async()=>({ok:true,json:async()=>({sid:'test-share',item_id:data.item_id,kind:'note',state:'ok',title:'完整作品标题',
    data:metadataFresh(data,{stats:{digg:null,comment:false,share:-1,collect:'12'}})})});
  await h.context.refreshShareTitle();
  assert.deepEqual(data.stats,{digg:0,comment:4});assert.equal(data.images,images);assert.equal(images[0],image);
  assert.equal(image.url,'https://media.test/one.jpg');assert.equal(image.filename,'完整作品标题_01.jpg');
  assert.match(h.nodes.shareEngagement.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.doesNotMatch(h.nodes.shareEngagement.innerHTML,/<dt>分享|<dt>收藏/);
  assert.doesNotMatch(h.nodes.mediaMetadata.innerHTML,/1920|1080|2:05|MP4/);
});
