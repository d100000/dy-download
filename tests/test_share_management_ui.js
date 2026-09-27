const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');
const share = fs.readFileSync('static/share.html', 'utf8');
const admin = fs.readFileSync('static/admin.html', 'utf8');
const slice = (html, start, end) => {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert.ok(a >= 0 && b > a);
  return html.slice(a, b);
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function viewHarness({storage = new Map(), reload = false, hidden = false, beacon = true} = {}) {
  const events = [], requests = [];
  const context = {
    S:{sid:'share_test',state:'pending'}, document:{hidden}, Blob,
    crypto:{randomUUID:()=> '11111111-1111-4111-a111-111111111111'},
    performance:{getEntriesByType:()=>[{type:reload?'reload':'navigate'}]},
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    navigator:{sendBeacon:(url,body)=>{events.push({url,body});return beacon;}},
    fetch:async(url,options)=>{requests.push({url,options});},
  };
  vm.runInNewContext(slice(share,'function track(kind, extra){','function toast(msg){'),context);
  return {context,events,requests,storage};
}

test('only a visible page records a view and returning to it does not double count', async () => {
  const h = viewHarness({hidden:true});
  h.context.recordPageView();
  assert.equal(h.events.length,0);
  h.context.document.hidden=false;
  h.context.recordPageView();
  h.context.S.state='ok';
  h.context.recordPageView();
  assert.equal(h.events.length,1);
  const body = JSON.parse(await h.events[0].body.text());
  assert.equal(body.kind,'page_view');
  assert.match(body.page_view_id,/^[A-Za-z0-9_-]{16,96}$/);
  assert.equal(h.events[0].url,'/api/share/share_test/event');
  for (const state of ['expired','notfound','takedown','dead','failed']) {
    const other=viewHarness();other.context.S.state=state;other.context.recordPageView();
    assert.equal(other.events.length,0);
  }
});

test('automatic pending-to-ready reload reuses its ID once; regular reload creates another visit', () => {
  const first=viewHarness();
  first.context.recordPageView();first.context.preservePageViewForReadyReload();
  const next=viewHarness({storage:first.storage,reload:true});
  next.context.crypto.randomUUID=()=> '22222222-2222-4222-a222-222222222222';
  assert.equal(next.context.pageViewID(),first.context.pageViewID());
  assert.equal(next.storage.size,0);
  const manual=viewHarness({storage:first.storage,reload:true});
  manual.context.crypto.randomUUID=()=> '33333333-3333-4333-a333-333333333333';
  assert.notEqual(manual.context.pageViewID(),first.context.pageViewID());
});

test('failed beacon queues a keepalive request and storage denial does not break visits', async () => {
  const h=viewHarness({beacon:false});
  h.context.sessionStorage.getItem=()=>{throw new Error('blocked');};
  h.context.recordPageView();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].options.keepalive,true);
  assert.equal(JSON.parse(h.requests[0].options.body).kind,'page_view');
});

function titleHarness(current = {}) {
  const nodes = {shareTitle:{textContent:''},shareTitleHint:{hidden:false}};
  const media = {url:'https://media.test/playing.mp4',currentTime:123};
  let resolve, calls=0, wxUpdates=0;
  const pending=new Promise(r=>{resolve=r;});
  const context={
    S:{sid:'share_test',item_id:'123456789',kind:'video',state:'ok',title:'旧标题',data:{title:'原标题',title_source:'share_text',video:media}},
    LANG:'zh',AbortController,setTimeout,clearTimeout,
    document:{hidden:false,title:'',getElementById:id=>nodes[id],querySelector:()=>null},
    updateWxShare:()=>{wxUpdates++;},
    fetch:async()=>{calls++;await pending;return {ok:true,json:async()=>({sid:'share_test',item_id:'123456789',kind:'video',state:'ok',title:'管理员新标题',data:{title:'原标题',title_source:'custom',title_status:'complete',video:{url:'https://media.test/replacement',filename:'new-title.mp4'}},...current})};},
  };
  vm.runInNewContext(slice(share,'function shareTitle(){','function countValue('),context);
  return {context,nodes,media,resolve,get calls(){return calls;},get wxUpdates(){return wxUpdates;}};
}

test('snapshot title refresh updates visible/card titles without replacing playing media', async () => {
  const h=titleHarness();
  const a=h.context.refreshShareTitle(),b=h.context.refreshShareTitle();
  assert.equal(h.calls,1);
  h.resolve();await Promise.all([a,b]);
  assert.equal(h.nodes.shareTitle.textContent,'管理员新标题');
  assert.equal(h.context.document.title,'管理员新标题');
  assert.equal(h.nodes.shareTitleHint.hidden,true);
  assert.equal(h.context.S.data.video,h.media);
  assert.equal(h.media.currentTime,123);
  assert.equal(h.media.filename,'new-title.mp4');
  assert.equal(h.wxUpdates,1);
});

test('gallery title refresh changes matching filenames while retaining existing image addresses', async () => {
  const h=titleHarness({kind:'note',data:{title:'新图集',title_source:'custom',images:[
    {index:1,url:'https://media.test/replacement.jpg',filename:'new_01.jpg'},
    {index:9,filename:'wrong_02.jpg'},
  ]}});
  h.context.S.kind='note';
  const images=[{index:1,url:'https://media.test/original.jpg',filename:'old_01.jpg'},{index:2,filename:'old_02.jpg'}];
  h.context.S.data.images=images;
  const pending=h.context.refreshShareTitle();h.resolve();await pending;
  assert.equal(h.context.S.data.images,images);
  assert.equal(images[0].filename,'new_01.jpg');assert.equal(images[0].url,'https://media.test/original.jpg');
  assert.equal(images[1].filename,'old_02.jpg');
});

test('wrong-work and hidden-page title refreshes cannot change the current share', async () => {
  const h=titleHarness({item_id:'different'});
  const request=h.context.refreshShareTitle();h.resolve();await request;
  assert.equal(h.context.S.title,'旧标题');
  h.context.document.hidden=true;await h.context.refreshShareTitle();assert.equal(h.calls,1);
});

test('share title ignores placeholders, prefers the effective title and localizes fallback', () => {
  const h=titleHarness();
  h.context.S.title='自定义标题';assert.equal(h.context.shareTitle(),'自定义标题');
  for (const title of [null,'','（无标题）','(无标题)','暂无标题','Untitled','No Title']) {
    h.context.S.title=title;h.context.S.data.title=title;h.context.S.data.content='真实正文';
    assert.equal(h.context.shareTitle(),'真实正文');
  }
  h.context.S.data.content='';h.context.S.data.platform='tiktok';h.context.LANG='en';
  assert.equal(h.context.shareTitle(),'TikTok post');
});

function adminHarness() {
  const nodes=new Map();
  const $=key=>{
    if(!nodes.has(key))nodes.set(key,{value:'',hidden:false,disabled:false,textContent:'',innerHTML:'',style:{},dataset:{},setAttribute(){},focus(){},showModal(){this.open=true;},close(){this.open=false;}});
    return nodes.get(key);
  };
  let save, rejectSave, writes=0, lastWrite;
  const row={id:'share_test',title:'<img src=x onerror=alert(1)>',original_title:'原始标题',custom_title:'已有修改',page_views:8,views:2,plays:1,downloads:0,cta_clicks:0,status:'ok',parse_status:'ready',created:1};
  const context={$,URLSearchParams,location:{search:'?lang=en'},esc,
    document:{cookie:'',documentElement:{lang:'en'},querySelectorAll:()=>[]},
    api:async(_path,options)=>{
      if(options?.method==='PATCH'){writes++;lastWrite={path:_path,body:JSON.parse(options.body)};return new Promise((resolve,reject)=>{save=resolve;rejectSave=reject;});}
      return {total:1,page_views:8,plays:1,cta:0,shares:[row]};
    },
    ago2:()=> 'now',toast(){},loadReports(){},loadShareConfig(){},loadPlayStats(){},loadTraffic(){},
  };
  vm.runInNewContext(slice(admin,'/* ---------------- 分享页管理 ---------------- */','/* ---------------- 播放诊断 ----------------'),context);
  return {context,$,row,get writes(){return writes;},get lastWrite(){return lastWrite;},save:()=>save({ok:true}),fail:()=>rejectSave(new Error('failed'))};
}

test('admin shows per-share and total page views and escapes stored titles', async () => {
  const h=adminHarness();await h.context.loadShares();
  assert.match(h.$('#shareStats').innerHTML,/Total page views.*8/s);
  assert.match(h.$('#sharesBody').innerHTML,/&lt;img src=x/);
  assert.doesNotMatch(h.$('#sharesBody').innerHTML,/<img/);
  assert.match(h.$('#sharesBody').innerHTML,/<td class="tnum">8<\/td><td class="tnum">2<\/td>/);
});

test('title editor prevents duplicate saves, preserves failed edits and supports cancel and restore', async () => {
  const h=adminHarness();await h.context.loadShares();h.context.editShareTitle('share_test');
  assert.equal(h.$('#shareTitleInput').value,'已有修改');
  h.$('#shareTitleInput').value='New title';
  const pending=h.context.saveShareTitle({preventDefault(){}});
  await h.context.saveShareTitle({preventDefault(){}});
  assert.equal(h.writes,1);assert.equal(h.$('#shareTitleCancel').disabled,true);
  h.context.closeShareTitle();assert.equal(h.$('#shareTitleDialog').open,true);
  h.fail();await pending;
  assert.equal(h.$('#shareTitleInput').value,'New title');
  assert.match(h.$('#shareTitleMessage').textContent,/Could not save/);
  assert.equal(h.$('#shareTitleSave').disabled,false);
  h.$('#shareTitleReset').onclick();assert.equal(h.$('#shareTitleInput').value,'');
  h.context.closeShareTitle();assert.equal(h.$('#shareTitleDialog').open,false);
  h.context.editShareTitle('share_test');h.$('#shareTitleInput').value='a'.repeat(301);
  await h.context.saveShareTitle({preventDefault(){}});assert.equal(h.writes,1);
  assert.match(h.$('#shareTitleMessage').textContent,/300/);
  h.$('#shareTitleReset').onclick();
  const restored=h.context.saveShareTitle({preventDefault(){}});
  assert.equal(h.lastWrite.path,'/api/admin/shares/share_test/title');
  assert.equal(h.lastWrite.body.title,'');
  h.save();await restored;
  assert.equal(h.$('#shareTitleDialog').open,false);
});
