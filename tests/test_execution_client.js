const test = require('node:test');
const assert = require('node:assert/strict');
globalThis.crypto ||= require('node:crypto').webcrypto;
const api = require('../assets/execution-client.js');

function storage() {
  const records = new Map();
  return {records, getItem: key => records.get(key), setItem: (key,value) => records.set(key,value)};
}
function response(payload, status=200) {
  return {status, json: async () => payload, clone: () => ({json: async () => payload})};
}

test('unknown network result reuses one request ID across reload without storing credentials', async () => {
  const saved=storage(), ids=[];
  const first=api.createFetch(async (_url,init) => { ids.push(JSON.parse(init.body).requestId); throw new Error('connection lost'); },
                              {storage:saved,identify:()=> 'request-1'});
  const init={method:'POST',body:JSON.stringify({amount:'1.00',password:'SYNTHETIC_SECRET'})};
  await assert.rejects(first('/api/deposit/fiat',init));
  const next=api.createFetch(async (_url,init) => { ids.push(JSON.parse(init.body).requestId); return response({ok:true,data:{ok:true}}); },
                            {storage:saved,identify:()=> 'request-2'});
  await next('/api/deposit/fiat',init);
  assert.deepEqual(ids,['request-1','request-1']);
  assert.doesNotMatch([...saved.records.values()].join(''), /SYNTHETIC_SECRET|password|amount/);
  await next('/api/deposit/fiat',init);
  assert.equal(ids[2],'request-2');
});

test('pending result keeps request ID and explicit IDs and reads remain unchanged', async () => {
  const calls=[],saved=storage();
  const fetch=api.createFetch(async (url,init) => {calls.push({url,init});return response({data:{pending:true}},202);},
                             {storage:saved,identify:()=> 'one'});
  const init={method:'POST',body:'{"id":"A"}'};
  await fetch('/api/run-case',init);
  await fetch('/api/run-case',init);
  assert.equal(JSON.parse(calls[0].init.body).requestId,JSON.parse(calls[1].init.body).requestId);
  const explicit={method:'POST',body:'{"requestId":"explicit"}'};
  await fetch('/api/run-case',explicit);
  assert.equal(calls[2].init,explicit);
  await fetch('/api/catalog',{});
  assert.equal(calls[3].init.body,undefined);
});

test('one completed request cannot erase another unresolved concurrent request across reload', async () => {
  const saved=storage(), pending=new Map();
  let sequence=0;
  const fetch=api.createFetch((_url,init)=>new Promise(resolve=>{
    const body=JSON.parse(init.body);
    pending.set(body.id,{resolve,id:body.requestId});
  }),{storage:saved,identify:()=> 'request-'+(++sequence)});
  const a=fetch('/api/run-case',{method:'POST',body:'{"id":"A"}'});
  const b=fetch('/api/run-case',{method:'POST',body:'{"id":"B"}'});
  const deadline=Date.now()+2000;
  while (pending.size<2) {
    assert.ok(Date.now()<deadline,'concurrent requests did not start');
    await new Promise(resolve=>setImmediate(resolve));
  }
  const unresolvedId=pending.get('B').id;
  pending.get('A').resolve(response({ok:true,data:{ok:true}}));
  await a;
  pending.get('B').resolve(response({ok:false},500));
  await b;
  let restored;
  const next=api.createFetch(async (_url,init)=>{
    restored=JSON.parse(init.body).requestId;
    return response({ok:true});
  },{storage:saved,identify:()=> 'unexpected-new-request'});
  await next('/api/run-case',{method:'POST',body:'{"id":"B"}'});
  assert.equal(restored,unresolvedId);
});

test('poll waits through queued and cancel_requested and reports terminal cancellation', async () => {
  const states=['queued','running','cancel_requested','cancelled'],seen=[];
  let time=0;
  const job=await api.pollJob(async () => response({ok:true,data:{status:states.shift()}}),'/api/job',
                             {onProgress:item=>seen.push(item.status),now:()=>time,wait:async ms=>{time+=ms;}});
  assert.equal(job.status,'cancelled');
  assert.deepEqual(seen,['queued','running','cancel_requested','cancelled']);
});

test('poll stops waiting after repeated query errors or a bounded wait without claiming success', async () => {
  let errors=0;
  await assert.rejects(api.pollJob(async()=>{errors++;throw new Error('offline');},'/api/job',{wait:async()=>{}}),/恢复查询/);
  assert.equal(errors,3);
  let time=0;
  await assert.rejects(api.pollJob(async()=>response({ok:true,data:{status:'running'}}),'/api/job',
                       {timeoutMs:10,intervalMs:5,now:()=>time,wait:async ms=>{time+=ms;}}),/等待超时/);
  await assert.rejects(api.pollJob(async()=>response({ok:true,data:{status:'unknown'}}),'/api/job'),/无法识别/);
});


test('terminal polling acknowledgement releases the id for a new user execution', async () => {
  const saved=storage(),ids=[];let sequence=0;
  const fetch=api.createFetch(async (_url,init)=>{ids.push(JSON.parse(init.body).requestId);return response({ok:true,data:{pending:true}});},
                              {storage:saved,identify:()=> 'run-'+(++sequence)});
  const body={id:'R01',background:true},init={method:'POST',body:JSON.stringify(body)};
  await fetch('/api/run-case',init);await fetch('/api/run-case',init);assert.equal(ids[0],ids[1]);
  await fetch.completeRequest('/api/run-case',body);
  await fetch('/api/run-case',init);assert.notEqual(ids[2],ids[0]);
});
