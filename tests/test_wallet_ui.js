const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const admin = fs.readFileSync('static/admin.html','utf8');
const home = fs.readFileSync('static/index.html','utf8');
function extract(text,start,end){return text.slice(text.indexOf(start),text.indexOf(end,text.indexOf(start)));}
test('money inputs use exact cents and reject negative, fractional cents and exponent syntax',()=>{
  const context=vm.createContext({walletText:()=> 'invalid'});
  vm.runInContext(extract(admin,'function moneyToCents','let billingLoaded'),context);
  for(const [input,expected] of [['0.03',3],['0',0],['12.5',1250],['1.01',101]])
    assert.equal(context.moneyToCents(input),expected);
  for(const input of ['-1','1.234','1e2','Infinity','','1,000'])assert.throws(()=>context.moneyToCents(input));
  assert.throws(()=>context.moneyToCents('1000.01',100000));
});
test('wallet history escapes admin notes and shows zero amounts',()=>{
  const context=vm.createContext({walletText:x=>x,yuan:c=>'¥'+(c/100).toFixed(2),esc:x=>String(x).replaceAll('<','&lt;').replaceAll('>','&gt;')});
  vm.runInContext(extract(admin,'function renderWalletLedger','async function reloadWallet'),context);
  const html=context.renderWalletLedger([{ts:1,event:'adjust',balance_delta:0,note:'<img src=x onerror=alert(1)>'}]);
  assert.match(html,/¥0\.00/);assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
});
test('homepage explains both balances, fees and zero pricing in Chinese and English',()=>{
  const context=vm.createContext({LANG:'zh'});
  vm.runInContext(extract(home,'function billingHint','async function refreshQuota'),context);
  const billing={wallet:{balance_cents:25,reserved_cents:3},parse_price_cents:3,transcript_price_cents:0};
  assert.match(context.billingHint(billing),/余额 ¥0.25.*预留 ¥0.03.*¥0.03\/次/);
  context.LANG='en';assert.match(context.billingHint(billing,'transcript'),/Balance ¥0.25.*¥0.00\/use; failures refunded/);
  assert.equal(context.billingHint({wallet:null}),'');
});
test('daily quota input accepts zero and whole numbers, rejects invalid limits',()=>{
  const context=vm.createContext({walletText:()=> 'invalid'});
  vm.runInContext(extract(admin,'function parseDailyQuota','let billingLoaded'),context);
  for(const n of ['0','20','10000'])assert.equal(context.parseDailyQuota(n),Number(n));
  for(const n of ['','-1','1.5','1e3','10001','NaN'])assert.throws(()=>context.parseDailyQuota(n));
});
test('signup prompts use configured quota and do not promise free uses for zero or unknown',()=>{
  const context=vm.createContext({LANG:'zh'});
  vm.runInContext(extract(home,'function quotaSignupText','function billingHint'),context);
  assert.match(context.quotaSignupText(25),/每天 25 次/);
  assert.match(context.quotaSignupText(25,'register'),/每天 25 次免费解析/);
  for(const daily of [0,null,undefined])assert.doesNotMatch(context.quotaSignupText(daily,'register'),/免费解析/);
  context.LANG='en';assert.match(context.quotaSignupText(25),/25\/day/);
  assert.doesNotMatch(context.quotaSignupText(0,'register'),/free parses/);
});
