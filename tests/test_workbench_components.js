const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../assets/workbench-components.js');
const execution = require('../assets/execution-client.js');
function storage() {const records = new Map(); return {records, getItem:key=>records.get(key)||null, setItem:(key,value)=>records.set(key,value), removeItem:key=>records.delete(key)};}
function node(value = '', tagName = 'INPUT') {const target = new EventTarget(); Object.assign(target, {value, tagName, textContent:'', title:'', hidden:false, dataset:{}, attributes:{}, setAttribute(key,value){this.attributes[key]=value;}}); return target;}
const deferred = () => {let resolve, reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject};};

test('feedback is local, accessible and text-only', () => {const status=node();ui.feedback(status).set('<img onerror=bad>',{kind:'warning'});assert.equal(status.textContent,'<img onerror=bad>');assert.equal(status.attributes.role,'status');assert.equal(status.attributes['aria-live'],'polite');assert.equal(status.dataset.kind,'warning');});

test('task restoration only queries the original execution; terminal releases its exact association', async () => {
  const saved=storage(), calls=[], completed=[], results=[];
  const fetcher=async(path,init)=>{calls.push({path,init});return{json:async()=>({ok:true,data:{status:'done',result:{ok:null,status:'incomplete',businessStatus:'WAITING'}}})};};
  fetcher.completeExecution=id=>completed.push(id);
  const options={pack:'synthetic_one',page:'jobs',scope:'user A',storage:saved,fetcher,execution};
  ui.createTaskController(options).remember({executionId:'job-original',secret:'must-not-save'});
  assert.doesNotMatch([...saved.records.values()].join(''),/must-not-save/);
  const resumed=ui.createTaskController({...options,onResult:value=>results.push(value)});resumed.restore();
  assert.equal(calls.length,0,'restore cannot submit or query implicitly');
  const result=await resumed.watch();
  assert.match(calls[0].path,/id=job-original$/);assert.equal(calls[0].init.method,undefined);assert.deepEqual(completed,['job-original']);assert.equal(result.ok,null);assert.equal(result.businessStatus,'WAITING');assert.equal(results.length,1);assert.equal(resumed.snapshot(),null);
});

test('task lookup failures retain identity and duplicate watch shares the query', async () => {
  const saved=storage(), states=[];let calls=0;
  const fetcher=async()=>{calls++;throw new Error('offline');};
  const task=ui.createTaskController({pack:'synthetic',page:'jobs',scope:'a',storage:saved,fetcher,execution,pollOptions:{wait:async()=>{}},onState:state=>states.push(state)});
  task.remember({executionId:'still-original'});const first=task.watch(),second=task.watch();assert.equal(first,second);
  await assert.rejects(first,/恢复查询/);assert.equal(calls,3);assert.equal(task.snapshot().executionId,'still-original');assert.equal(states.at(-1),'unavailable');assert.match([...saved.records.values()].join(''),/still-original/);
  assert.throws(()=>task.remember({executionId:'replacement'}),/不能替换/);
});

test('default protected fetch retains its completion method and render errors do not change terminal knowledge', async () => {
  const original=global.fetch,completed=[],states=[];
  global.fetch=Object.assign(async()=>({json:async()=>({ok:true,data:{status:'done',result:{ok:null}}})}),{completeExecution:id=>completed.push(id)});
  try {
    const task=ui.createTaskController({pack:'synthetic',page:'default',scope:'a',storage:storage(),execution,onState:state=>states.push(state),onResult:()=>{throw new Error('render failed');}});
    task.remember({executionId:'default-original'});await assert.rejects(task.watch(),/render failed/);assert.deepEqual(completed,['default-original']);assert.equal(task.snapshot(),null);assert.equal(states.at(-1),'terminal');
  } finally {global.fetch=original;}
});

test('expired task evidence yields incomplete and never becomes business success', async () => {
  const task=ui.createTaskController({pack:'synthetic',page:'jobs',scope:'expired',storage:storage(),execution,fetcher:async()=>({json:async()=>({ok:true,data:{status:'done',resultExpired:true,result:{ok:true}}})})});
  task.remember({executionId:'expired-job'});const result=await task.watch();assert.equal(result.ok,null);assert.equal(result.status,'incomplete');assert.equal(result.resultExpired,true);
});

test('task records are isolated by Pack page and scope; unavailable storage is explicit', () => {
  const saved=storage(), base={pack:'one',page:'jobs',scope:'a',storage:saved,execution,fetcher:async()=>{}};
  ui.createTaskController(base).remember({executionId:'one'});
  for(const change of [{pack:'two'},{page:'other'},{scope:'b'}])assert.equal(ui.createTaskController({...base,...change}).restore(),null);
  const notices=[];ui.createTaskController({...base,storage:{setItem(){throw new Error();}},onNotice:msg=>notices.push(msg)}).remember({executionId:'memory-only'});assert.match(notices[0],/刷新后无法恢复/);
});

test('form input saves only whitelisted fields immediately and pending options preserve previous selection', () => {
  const saved=storage(), fields={label:node('old'),choice:node('selected','SELECT')};
  let form=ui.createFormContext({pack:'one',page:'form',scope:'a',fields,storage:saved});form.save();
  fields.label.value='';fields.choice.value='';form=ui.createFormContext({pack:'one',page:'form',scope:'a',fields,storage:saved});form.restore();form.setPending(['choice']);form.bind();
  fields.choice.value='';fields.label.value='focused latest';fields.label.dispatchEvent(new Event('input'));
  const record=JSON.parse([...saved.records.values()][0]);assert.deepEqual(record.values,{label:'focused latest',choice:'selected'});
  form.setPending([]);fields.choice.dispatchEvent(new Event('change'));assert.equal(JSON.parse([...saved.records.values()][0]).values.choice,'','loaded empty is an explicit empty');
  form.dispose();fields.label.value='not saved';fields.label.dispatchEvent(new Event('input'));assert.equal(JSON.parse([...saved.records.values()][0]).values.label,'focused latest');
});

test('form scope switches clear previous identity fields and versions do not reuse old values', () => {
  const saved=storage(),fields={label:node('scope A')};const base={pack:'one',page:'form',scope:'a',fields,storage:saved};
  const form=ui.createFormContext(base);form.save();form.setScope('b');assert.equal(fields.label.value,'');fields.label.value='scope B';form.save();form.setScope('a');assert.equal(fields.label.value,'scope A');
  const notices=[];const newer=ui.createFormContext({...base,version:2,onNotice:message=>notices.push(message)});assert.deepEqual(newer.restore(),{});assert.match(notices[0],/版本已变化/);
  for(const extra of [{pack:'two'},{page:'different'}])assert.deepEqual(ui.createFormContext({...base,...extra}).restore(),{});
});

test('form storage failures and invalid white lists produce an explicit fallback', () => {
  const notices=[],form=ui.createFormContext({pack:'one',page:'form',scope:'a',fields:{input:node('value')},storage:{getItem(){throw new Error();},setItem(){throw new Error();}},onNotice:message=>notices.push(message)});form.restore();form.save();assert.equal(notices.length,2);assert.throws(()=>form.setPending(['secret']),/白名单/);
});

test('readonly list discards unordered replies and replies from an old scope', async () => {
  let scope='A';const requests=[];const list=ui.createReadonlyList({scope:()=>scope,load:args=>{const value=deferred();requests.push({args,...value});return value.promise;},keyOf:row=>row.key,rowsOf:data=>data.rows,cursorOf:data=>data.next});
  const older=list.query({filters:{search:'old'}}),newer=list.query({filters:{search:'new'}});
  requests[1].resolve({rows:[{key:'new'}],next:'cursor'});await newer;requests[0].resolve({rows:[{key:'old'}],next:'old'});assert.deepEqual(await older,{stale:true});assert.equal(list.snapshot().rows[0].key,'new');assert.equal(requests[0].args.signal.aborted,true);
  const switching=list.query();scope='B';requests[2].resolve({rows:[{key:'old-scope'}]});assert.deepEqual(await switching,{stale:true});
});

test('readonly pagination passes opaque cursors unchanged and resets when filters or page size changes', async () => {
  const opaque='opaque+/==';const calls=[];
  const list=ui.createReadonlyList({load:async args=>{calls.push(args);return{rows:[{key:'same',value:calls.length}],next:opaque};},keyOf:row=>row.key,rowsOf:data=>data.rows,cursorOf:data=>data.next});
  await list.query({filters:{tag:'A'},limit:'7'});await list.query({filters:{tag:'A'},limit:'7',append:true});assert.equal(calls[1].cursor,opaque);assert.equal(list.snapshot().rows.length,1);assert.equal(list.snapshot().rows[0].value,2);
  await list.query({filters:{tag:'B'},limit:'7',append:true});assert.equal(calls[2].cursor,null);
  await list.query({filters:{tag:'B'},limit:'13',append:true});assert.equal(calls[3].cursor,null);
});

test('read guard invalidation rejects a response even after returning to the same label', () => {let scope='A:1';const guard=ui.createReadGuard(()=>scope),token=guard.begin();scope='A:2';assert.equal(guard.current(token),false);scope='A:1';guard.invalidate();assert.equal(guard.current(token),false);});

function dialogFixture() {
  const doc={activeElement:null};const opener={focus(){doc.activeElement=this;}},first={tabIndex:0,focus(){doc.activeElement=this;}},last={tabIndex:0,focus(){doc.activeElement=this;}};
  class Dialog extends EventTarget {open=false;ownerDocument=doc;showModal(){this.open=true;}close(){this.open=false;this.dispatchEvent(new Event('close'));}querySelectorAll(){return[first,last];}getBoundingClientRect(){return{left:0,right:100,top:0,bottom:100};}}
  const dialog=new Dialog(),body={style:{overflow:'auto'}},status=node(),content=node();
  return{dialog,doc,opener,first,last,body,status,content,controller:ui.createDetailsDialog({dialog,body,status,content,project:value=>'Visible '+value.allowed})};
}
test('details preserve a confirmed result after a read failure; retry and closing reject late data', async () => {
  const f=dialogFixture(),pending=deferred();
  await f.controller.open(f.opener,{key:'A',initial:{allowed:'confirmed',secret:'hidden'},load:async()=>{throw new Error('offline');}});
  assert.equal(f.content.textContent,'Visible confirmed');assert.match(f.status.textContent,/已保留/);assert.equal(f.body.style.overflow,'hidden');assert.equal(f.doc.activeElement,f.first);
  const reading=f.controller.open(f.opener,{key:'A',load:()=>pending.promise});f.controller.close();pending.resolve({allowed:'too late'});await reading;
  assert.equal(f.content.textContent,'Visible confirmed');assert.equal(f.body.style.overflow,'auto');assert.equal(f.doc.activeElement,f.opener);
  await f.controller.open(f.opener,{key:'B'});assert.equal(f.content.textContent,'尚未取得详情。');
});
test('details trap focus at both ends and Escape returns focus to the opener', async () => {
  const f=dialogFixture();await f.controller.open(f.opener,{key:'A'});f.last.focus();let event=new Event('keydown',{cancelable:true});Object.assign(event,{key:'Tab',shiftKey:false});f.dialog.dispatchEvent(event);assert.equal(event.defaultPrevented,true);assert.equal(f.doc.activeElement,f.first);
  event=new Event('keydown',{cancelable:true});Object.assign(event,{key:'Tab',shiftKey:true});f.dialog.dispatchEvent(event);assert.equal(f.doc.activeElement,f.last);
  const cancel=new Event('cancel',{cancelable:true});f.dialog.dispatchEvent(cancel);assert.equal(cancel.defaultPrevented,true);assert.equal(f.dialog.open,false);assert.equal(f.doc.activeElement,f.opener);
});

test('exact display never casts numeric text, keeps raw evidence and refuses already parsed Number', () => {
  const raw='900719925474099312345.1234567890123456789000';assert.equal(ui.formatExactText(raw),raw);assert.equal(ui.formatExactText(raw,{trimFractionZeros:true}),'900719925474099312345.1234567890123456789');assert.equal(ui.formatExactText('1e-18',{trimFractionZeros:true}),'1e-18');assert.equal(ui.formatExactText(null),'—');assert.throws(()=>ui.formatExactText(9007199254740993),/不能恢复/);const target=node();ui.presentExact(target,raw,{trimFractionZeros:true});assert.equal(target.title,raw);
});
test('time display requires an explicit valid timezone and leaves timezone-free evidence unchanged', () => {
  const raw='2026-10-20T16:01:02.123456Z';assert.equal(ui.formatTime(raw,{timeZone:'Asia/Shanghai'}),'2026-10-21 00:01:02');assert.equal(ui.formatTime(raw,{timeZone:'UTC'}),'2026-10-20 16:01:02');assert.equal(ui.formatTime('2026-10-20T16:01:02',{timeZone:'UTC'}),'2026-10-20T16:01:02');assert.throws(()=>ui.formatTime(raw),/显式/);assert.throws(()=>ui.formatTime(raw,{timeZone:'Invalid/Zone'}));
});
test('copy reports success or permission denial next to its own control', async () => {const first=node(),second=node();let actual;assert.deepEqual(await ui.copyExact('000123',{status:first,clipboard:{writeText:async value=>{actual=value;}}}),{ok:true});assert.equal(actual,'000123');assert.equal(first.textContent,'已复制。');assert.deepEqual(await ui.copyExact('000123',{status:second,clipboard:{writeText:async()=>{throw new Error('denied');}}}),{ok:false});assert.match(second.textContent,/denied/);assert.equal(first.textContent,'已复制。');});
test('an older failed copy cannot overwrite a newer success', async () => {const status=node(),old=deferred();const first=ui.copyExact('old',{status,clipboard:{writeText:()=>old.promise}});await ui.copyExact('new',{status,clipboard:{writeText:async()=>{}}});old.reject(new Error('old failure'));await first;assert.equal(status.textContent,'已复制。');});
