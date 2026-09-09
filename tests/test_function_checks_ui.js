const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function ui() {
  const html = fs.readFileSync('static/admin.html', 'utf8');
  const code = html.split('/* ---------------- 功能检查 ---------------- */')[1].split('/* ---------------- 视频解析服务 ---------------- */')[0];
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {innerHTML:'', textContent:'', className:'', style:{display:''}, querySelectorAll:()=>[], insertAdjacentHTML(_position,html){this.innerHTML+=html;}});
    return nodes.get(key);
  };
  const context = { $, document:{cookie:'',documentElement:{lang:'en'},querySelector:$,querySelectorAll:()=>[]},
    location:{search:'?lang=en'},URLSearchParams,setTimeout,clearTimeout,
    esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    api:()=>Promise.resolve({}),toast:()=>{} };
  vm.createContext(context);
  vm.runInContext(code+'\nglobalThis.FC={fcRows,renderFunctionChecks};', context);
  return {nodes,$,...context.FC};
}
test('diagnostic result escapes untrusted post titles and share titles',()=>{
  const x=ui();
  const attack='<img src=x onerror=alert(1)>';
  const html=x.fcRows([{id:'metadata',status:'pass',code:'metadata_ok',value:{title:attack,stats:{digg:0}}}]);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('Likes: 0'));
  x.renderFunctionChecks({checks:[],job:{},shares:{scanned:1,items:[{sid:'safe123',title:attack,missing:['title']}]}});
  assert.ok(!x.$('#checksShares').innerHTML.includes('<img'));
});
test('configured parser remains untested and fallback cannot hide primary failure',()=>{
  const x=ui();
  const pending=x.fcRows([{id:'parser',status:'pending',code:'parser_configured'}]);
  assert.ok(pending.includes('Not tested'));
  x.renderFunctionChecks({checks:[],job:{state:'done',started_at:1,checks:[
    {id:'parser',status:'fail',code:'parser_auth'},
    {id:'metadata',status:'pass',code:'metadata_ok'}]},shares:{items:[],scanned:0}});
  assert.equal(x.$('#checksJobBadge').textContent,'Failed');
});
test('active jobs keep controls disabled until finished and cooldown expires',()=>{
  const x=ui();
  const state={checks:[],job:{state:'running',started_at:1,checks:[]},shares:{items:[],scanned:0}};
  assert.equal(x.renderFunctionChecks(state),true);
  assert.equal(x.$('#checksRun').disabled,true);
  state.job={state:'done',finished_at:Math.floor(Date.now()/1000),started_at:1,checks:[]};
  assert.equal(x.renderFunctionChecks(state),true);
  state.job.finished_at-=20;
  assert.equal(x.renderFunctionChecks(state),false);
  assert.equal(x.$('#checksRun').disabled,false);
});
