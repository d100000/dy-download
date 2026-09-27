const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const admin = fs.readFileSync('static/admin.html','utf8');
const home = fs.readFileSync('static/index.html','utf8');
const share = fs.readFileSync('static/share.html','utf8');
function extract(text,start,end){const a=text.indexOf(start),b=text.indexOf(end,a);assert.ok(a>=0&&b>a,`${start} .. ${end}`);return text.slice(a,b);}
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function harness(){
  const nodes=new Map();
  const node=id=>{
    if(nodes.has(id))return nodes.get(id);
    const el={value:'',textContent:'',innerHTML:'',disabled:false,open:false,listeners:{},style:{},
      reset(){},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.({});},addEventListener(type,fn){this.listeners[type]=fn;}};
    nodes.set(id,el);return el;
  };
  let next=0;const requests=[],queue=[];
  const c=vm.createContext({$:sel=>node(sel.slice(1)),walletText:x=>x,creditText:x=>x,esc,window:{},crypto:{randomUUID:()=>`request-${++next}`},toast(){},loadUsers(){},
    api:async(path,opts)=>{requests.push({path,...opts});const out=queue.shift();if(out instanceof Error)throw out;return await out;}});
  vm.runInContext(extract(admin,'function parseDailyQuota','let billingLoaded'),c);
  vm.runInContext(extract(admin,'/* 单个用户的免费额度','/* ---------------- 解析日志'),c);
  return {c,node,requests,queue};
}
const quota=(extra={})=>({email:'user@example.test',daily_limit:null,daily_global:20,daily_effective:20,quota_version:3,free_used:4,free_remaining:16,...extra});
const credits=(extra={})=>({email:'user@example.test',balance:9,reserved:2,spent:1,version:7,ledger:[],...extra});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const submit=el=>el.onsubmit({preventDefault(){}});

test('credit quantities reject fractions, exponent syntax, unsafe and negative values',()=>{
  const h=harness();for(const value of ['0','1','1000000000'])assert.equal(h.c.parseCreditUnits(value),Number(value));
  for(const value of ['','-1','0.5','1e3','Infinity','1000000001'])assert.throws(()=>h.c.parseCreditUnits(value));
});

test('daily editor distinguishes global default and an explicit zero, with usage preserved in summary',async()=>{
  const h=harness();h.queue.push(quota());h.c.window.editUserQuota(1);await tick();
  assert.equal(h.node('userQuotaMode').value,'default');assert.equal(h.node('userQuotaLimit').disabled,true);
  assert.match(h.node('userQuotaSummary').textContent,/4 \/ 16/);
  h.node('userQuotaMode').value='custom';h.node('userQuotaMode').onchange();h.node('userQuotaLimit').value='0';h.queue.push({});await submit(h.node('userQuotaForm'));
  assert.deepEqual(JSON.parse(h.requests[1].body),{daily_limit:0,expected_version:3});assert.equal(h.requests[1].method,'PATCH');
  h.queue.push(quota({daily_limit:0,daily_effective:0,free_remaining:0}));h.c.window.editUserQuota(1);await tick();
  assert.equal(h.node('userQuotaMode').value,'custom');assert.equal(h.node('userQuotaLimit').value,0);
  h.node('userQuotaMode').value='default';h.node('userQuotaMode').onchange();h.queue.push({});await submit(h.node('userQuotaForm'));
  assert.equal(JSON.parse(h.requests[3].body).daily_limit,null);
});

test('late daily responses cannot replace another user or a closed editor',async()=>{
  const h=harness(),late=deferred();h.queue.push(late.promise,quota({email:'second@example.test',quota_version:9}));
  h.c.window.editUserQuota(1);h.c.window.editUserQuota(2);await tick();late.resolve(quota({email:'first@example.test'}));await tick();
  assert.match(h.node('userQuotaSummary').textContent,/second/);
  h.node('userQuotaMode').value='custom';h.node('userQuotaLimit').value='10';h.queue.push({});await submit(h.node('userQuotaForm'));
  assert.equal(h.requests.at(-1).path,'/api/admin/users/2/quota');assert.equal(JSON.parse(h.requests.at(-1).body).expected_version,9);
  const closed=deferred();h.queue.push(closed.promise);h.c.window.editUserQuota(3);h.node('userQuotaCancel').onclick();closed.resolve(quota());await tick();
  assert.equal(h.node('userQuotaSave').disabled,true);
});

test('daily conflict retains typed value and blocks another save until refresh',async()=>{
  const h=harness();h.queue.push(quota());h.c.window.editUserQuota(1);await tick();
  h.node('userQuotaMode').value='custom';h.node('userQuotaLimit').value='15';
  h.queue.push(Object.assign(new Error('stale'),{status:409}));await submit(h.node('userQuotaForm'));
  assert.equal(h.node('userQuotaLimit').value,'15');assert.equal(h.node('userQuotaSave').disabled,true);assert.equal(h.node('userQuotaReload').disabled,false);
  const count=h.requests.length;await submit(h.node('userQuotaForm'));assert.equal(h.requests.length,count);
});

test('ambiguous credit save retry preserves idempotency key and saving prevents duplicate requests',async()=>{
  const h=harness();h.queue.push(credits());h.c.window.editUserCredits(5);await tick();
  h.node('creditsMode').value='add';h.node('creditsUnits').value='8';h.node('creditsSource').value='purchase';h.node('creditsNote').value='paid';
  const pending=deferred();h.queue.push(pending.promise);const first=submit(h.node('creditsForm'));await tick();
  await submit(h.node('creditsForm'));assert.equal(h.requests.length,2);assert.equal(h.node('creditsCancel').disabled,true);
  let prevented=false;h.node('creditsDialog').listeners.cancel({preventDefault(){prevented=true;}});assert.equal(prevented,true);
  pending.reject(new Error('network interrupted'));await first;assert.equal(h.node('creditsUnits').value,'8');
  h.queue.push({});await submit(h.node('creditsForm'));
  const a=JSON.parse(h.requests[1].body),b=JSON.parse(h.requests[2].body);assert.equal(a.request_id,b.request_id);assert.equal(a.source,'purchase');assert.equal(a.units,8);assert.equal(a.expected_version,7);
});

test('credit version conflicts require a fresh balance before saving again',async()=>{
  const h=harness();h.queue.push(credits());h.c.window.editUserCredits(5);await tick();
  h.node('creditsMode').value='add';h.node('creditsUnits').value='5';h.node('creditsSource').value='admin';
  h.queue.push(Object.assign(new Error('stale'),{status:409}));await submit(h.node('creditsForm'));
  assert.equal(h.node('creditsSave').disabled,true);assert.equal(h.node('creditsReload').disabled,false);assert.equal(h.node('creditsUnits').value,'5');
  h.queue.push(credits({version:8}));await h.c.reloadCredits();assert.equal(h.node('creditsSave').disabled,false);
});

test('credit editor excludes purchase source when setting a balance and escapes ledger notes',async()=>{
  const h=harness();h.queue.push(credits({ledger:[{ts:1,event:'share_reward',balance_delta:0,reserved_delta:0,spent_delta:0,note:'<img onerror=evil()>'}]}));h.c.window.editUserCredits(2);await tick();
  assert.match(h.node('creditsLedger').innerHTML,/&lt;img/);assert.doesNotMatch(h.node('creditsLedger').innerHTML,/<img/);assert.match(h.node('creditsLedger').innerHTML,/>0</);
  h.node('creditsMode').value='set';h.node('creditsSource').value='purchase';h.node('creditsMode').onchange();assert.equal(h.node('creditsSource').disabled,true);
  h.node('creditsUnits').value='0';h.queue.push({});await submit(h.node('creditsForm'));const body=JSON.parse(h.requests.at(-1).body);assert.equal(body.source,'admin');assert.equal(body.units,0);
});

test('credit load failure cannot submit a stale user balance and remains reloadable',async()=>{
  const h=harness();h.queue.push(new Error('not found'));h.c.window.editUserCredits(99);await tick();
  assert.equal(h.node('creditsSave').disabled,true);assert.equal(h.node('creditsReload').disabled,false);assert.equal(h.node('creditsMessage').textContent,'not found');
  await submit(h.node('creditsForm'));assert.equal(h.requests.length,1);
});

test('homepage separately labels long-lived credits and keeps transcript charging separate',()=>{
  const c=vm.createContext({LANG:'zh',uiText:s=>s});
  vm.runInContext(extract(home,'function generalCreditsHint','async function refreshQuota'),c);
  assert.match(c.generalCreditsHint({balance:0,reserved:2}),/通用额度 0 次（长期有效）.*预留 2 次/);
  const b={wallet:{balance_cents:3,reserved_cents:0},credits:{balance:2},parse_price_cents:3,transcript_price_cents:3};
  assert.match(c.billingHint(b),/免费次数和通用额度用完后/);assert.doesNotMatch(c.billingHint(b,'transcript'),/通用额度/);
  c.LANG='en';assert.match(c.generalCreditsHint({balance:5}),/General credits: 5 \(no expiry\)/);assert.doesNotMatch(c.billingHint(b,'transcript'),/credits/);
});

test('referral CTA preserves only a validated local share ID and cannot navigate to an external URL',()=>{
  const c=vm.createContext({S:{sid:'Abc123_'},encodeURIComponent});vm.runInContext(extract(share,'function shareReferralURL','function render('),c);
  assert.equal(c.shareReferralURL(),'/?ref=Abc123_');
  for(const sid of ['javascript:alert(1)','//evil.test','ab','a?x=bad','a'.repeat(65)]){c.S.sid=sid;assert.equal(c.shareReferralURL(),'/');}
  assert.match(share,/<a class="cta" href="\$\{esc\(shareReferralURL\(\)\)\}"/);
});
