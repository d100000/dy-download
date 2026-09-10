const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(lang=0){
  const html=fs.readFileSync('static/admin.html','utf8');
  const source=html.split('/* ---------------- 解析日志 ---------------- */')[1].split('/* ---------------- /解析日志 ---------------- */')[0];
  const nodes=new Map();
  const $=key=>{if(!nodes.has(key))nodes.set(key,{value:'',innerHTML:'',textContent:'',disabled:false,open:false,showModal(){this.open=true;},close(){this.open=false;this.onclose?.();}});return nodes.get(key);};
  const context={$ ,FC_LANG:lang,document:{querySelectorAll:()=>[]},Date,URLSearchParams,encodeURIComponent,
    esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    api:async()=>({logs:[],total:0,retention_days:30,messages:{}})};
  vm.createContext(context);vm.runInContext(source,context);return context;
}
const row={id:'a'.repeat(24),ts:1,duration_ms:50,entry:'web',status:'partial',code:'partial',version:'1.30.0',missing:['digg','comment'],events:[]};
test('list and detail escape external metadata and unexpected event levels',()=>{
  const x=setup();const attack='<img src=x onerror=alert(1)>';
  x.renderParseLogRows([{...row,item_id:attack,platform:attack}]);
  assert.ok(!x.$('#parseLogBody').innerHTML.includes('<img'));
  x.renderParseLogDetail({...row,reference:attack,events:[{stage:attack,code:attack,level:attack,ms:1}]});
  assert.ok(!x.$('#parseLogSummary').innerHTML.includes('<img'));
  assert.ok(!x.$('#parseLogTimeline').innerHTML.includes('<img'));
});
test('partial is visible separately from success with localized missing fields',()=>{
  const x=setup(1);x.renderParseLogRows([row]);
  const html=x.$('#parseLogBody').innerHTML;
  assert.ok(html.includes('Parsed · incomplete'));
  assert.ok(html.includes('Likes · Comments'));
  assert.ok(!html.includes('暂无'));
});
test('empty and failure states clear stale rows and restore refresh',async()=>{
  const x=setup();await x.loadParseLogs();
  assert.ok(x.$('#parseLogState').textContent.includes('历史处理过程无法补录'));
  x.$('#parseLogBody').innerHTML='stale';x.api=async()=>{throw new Error('secret upstream error');};
  await x.loadParseLogs();
  assert.equal(x.$('#parseLogBody').innerHTML,'');
  assert.equal(x.$('#parseLogRefresh').disabled,false);
  assert.equal(x.$('#parseLogNext').disabled,true);
  assert.ok(!x.$('#parseLogState').textContent.includes('secret'));
});
test('late list response cannot overwrite newer filter results',async()=>{
  const x=setup();let done; x.api=()=>new Promise(resolve=>done=resolve);
  const old=x.loadParseLogs();
  x.api=async()=>({logs:[{...row,item_id:'new-post'}],total:1,messages:{},retention_days:30});
  await x.loadParseLogs();done({logs:[{...row,item_id:'old-post'}],total:1,messages:{},retention_days:30});await old;
  assert.ok(x.$('#parseLogBody').innerHTML.includes('new-post'));
  assert.ok(!x.$('#parseLogBody').innerHTML.includes('old-post'));
});
test('late detail response does not repopulate a closed dialog',async()=>{
  const x=setup();let done;x.api=()=>new Promise(resolve=>done=resolve);
  const pending=x.openParseLog(row.id);x.$('#parseLogDialog').close();
  done({log:row,messages:{}});await pending;
  assert.equal(x.$('#parseLogSummary').innerHTML,'');
});
test('detail loading errors support explicit retry without exposing server messages',async()=>{
  const x=setup();x.api=async()=>{throw new Error('anytocopy secret');};
  await x.openParseLog(row.id);
  assert.ok(x.$('#parseLogDetailState').textContent.includes('刷新重试'));
  assert.equal(x.$('#parseLogDetailRefresh').disabled,false);
  x.api=async()=>({log:row,messages:{}});await x.openParseLog(row.id);
  assert.equal(x.$('#parseLogDetailState').textContent,'');
  assert.ok(x.$('#parseLogSummary').innerHTML.includes(row.id));
});
