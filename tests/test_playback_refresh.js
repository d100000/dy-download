const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('./helpers/localized-vm');
const html = fs.readFileSync('static/index.html','utf8');
const source = html.slice(html.indexOf('const _playbackRefreshes'), html.indexOf('function useProxyFallback'));
function harness(refresh){
  const video = {url:'https://v3.douyinvod.com/old.mp4',source:'parser',proxy_url:'/backup',download_refresh_url:'/signed'};
  const v = {dataset:{itemId:'test',direct:video.url,source:'parser',proxy:'/backup',playIndex:'0'},
    currentTime:0,paused:true,isConnected:true,load(){this.loads=(this.loads||0)+1;},play(){throw Error('no autoplay');}};
  const labels=[];
  const c = {_videos:{test:video}, _batch:[], IS_WECHAT_UA:false,
    refreshVideoDownloadLink:refresh, videoMetadataScope:()=>({}),
    setVideoMetadataValue:(_s,_k,v)=>labels.push(v),markVideoMetadataUnavailable:()=>{v.unavailable=true;},Date};
  vm.runInNewContext(source,c);
  return {c,v,video,labels};
}
test('homepage expiry refreshes once, updates download model, and never starts paused video', async()=>{
  let calls=0;
  const h=harness(async model=>{calls++;model.url='https://v3.douyinvod.com/new.mp4';return model.url;});
  h.c.videoProxyFallback(h.v);h.c.videoProxyFallback(h.v);
  await new Promise(setImmediate);
  assert.equal(calls,1);assert.equal(h.v.src,h.video.url);
  assert.equal(h.v.dataset.refreshTried,'1');
  h.c.videoProxyFallback(h.v);
  assert.equal(h.v.src,'/backup');assert.equal(calls,1);
});
test('homepage refresh failure uses backup and gives up after its failure',async()=>{
  let calls=0;const h=harness(async()=>{calls++;throw Error('unavailable');});
  h.c.videoProxyFallback(h.v);await new Promise(setImmediate);
  assert.equal(h.v.src,'/backup');h.c.videoProxyFallback(h.v);
  assert.equal(h.v.unavailable,true);assert.equal(calls,1);
});
test('a replaced homepage result is not changed by a late refresh',async()=>{
  let complete;const h=harness(()=>new Promise(resolve=>{complete=resolve;}));
  h.c.videoProxyFallback(h.v);h.v.isConnected=false;complete('https://v3.douyinvod.com/new.mp4');
  await new Promise(setImmediate);
  assert.equal(h.v.loads,undefined);
});
