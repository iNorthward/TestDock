const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
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

for (const reload of [false, true]) {
  test(`an unknown network result without an execution ID is retained beyond 24 hours${reload ? ' after reload' : ''}`, async () => {
    const saved = storage(), ids = [];
    let time = 1000, sequence = 0;
    const original = async (_url, init) => {
      ids.push(JSON.parse(init.body).requestId);
      if (ids.length === 1) throw new Error('connection lost before execution ID');
      return response({ok: true, data: {pending: true}});
    };
    const create = () => api.createFetch(original, {storage: saved, now: () => time, identify: () => 'retained-'+(++sequence)});
    const first = create();
    const init = {method: 'POST', body: '{"password":"SYNTHETIC_SECRET","background":true}'};
    await assert.rejects(first('/api/run-case', init), /connection lost/);
    time += 25*60*60*1000;
    const next = reload ? create() : first;
    assert.equal(ids.length, 1, 'restoring a client must not dispatch a request automatically');
    await next('/api/run-case', init);
    assert.equal(ids[0], ids[1]);
    const records = JSON.parse(saved.getItem('platform.executionRequests.v1'));
    assert.deepEqual(Object.values(records), [{id: 'retained-1'}]);
    assert.doesNotMatch([...saved.records.values()].join(''), /SYNTHETIC_SECRET|password|expires/);
  });
}

test('legacy expired records retain their request IDs and can still be acknowledged by execution ID', async () => {
  const saved = storage(), ids = [];
  const url = '/api/run-case', body = {id: 'LEGACY-L01', background: true, confirmLive: 'LEGACY-L01'};
  const key = require('node:crypto').createHash('sha256').update(url+'\n'+JSON.stringify(body)).digest('hex');
  saved.setItem('platform.executionRequests.v1', JSON.stringify({
    [key]: {id: 'legacy-request', expires: 1000, executionId: 'legacy-job'}
  }));
  const fetch = api.createFetch(async (_url, init) => {
    ids.push(JSON.parse(init.body).requestId);
    return response({ok: true, data: {pending: true, executionId: 'legacy-job'}});
  }, {storage: saved, now: () => 1000+25*60*60*1000, identify: () => 'new-request'});
  await fetch(url, {method: 'POST', body: JSON.stringify(body)});
  assert.equal(ids[0], 'legacy-request');
  assert.deepEqual(JSON.parse(saved.getItem('platform.executionRequests.v1'))[key], {id: 'legacy-request', executionId: 'legacy-job'});
  await fetch.completeExecution('legacy-job');
  await fetch(url, {method: 'POST', body: JSON.stringify(body)});
  assert.equal(ids[1], 'new-request');
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

const panel = fs.readFileSync(path.join(__dirname, '../test-platform/panel.html'), 'utf8');
const cleanup = panel.match(/await fetch\.completeExecution\?\.[^\n]+/)[0];
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const finishFromPanel = new AsyncFunction('fetch', 'current', cleanup);

for (const batch of [false, true]) {
  test(`panel terminal acknowledgement releases a LIVE ${batch ? 'batch' : 'case'} after reload`, async () => {
    const saved = storage(), ids = [];
    let sequence = 0;
    const original = async (_url, init) => {
      ids.push(JSON.parse(init.body).requestId);
      return response({ok: true, data: {pending: true, executionId: 'live-job-'+ids.at(-1)}});
    };
    const create = () => api.createFetch(original, {storage: saved, identify: () => 'live-'+(++sequence)});
    const fetch = create();
    const url = batch ? '/api/run' : '/api/run-case';
    const body = batch ? {suite: 'AUDIT', mode: 'live', background: true, confirmLive: 'AUDIT'}
                       : {id: 'AUDIT-L01', background: true, confirmLive: 'AUDIT-L01'};
    const init = {method: 'POST', body: JSON.stringify(body)};
    await fetch(url, init);
    await fetch(url, init);
    assert.equal(ids[0], ids[1]);
    const restored = create();
    await finishFromPanel(restored, {jobId: 'live-job-'+ids[0]});
    await restored(url, init);
    assert.notEqual(ids[0], ids[2]);
    assert.doesNotMatch([...saved.records.values()].join(''), /confirmLive|AUDIT/);
  });
}

test('a quickly finished background submission remains associated until terminal acknowledgement', async () => {
  const saved = storage(), ids = [];
  let sequence = 0;
  const fetch = api.createFetch(async (_url, init) => {
    ids.push(JSON.parse(init.body).requestId);
    return response({ok: true, data: {pending: false, executionId: 'instant-job', executionStatus: 'done'}});
  }, {storage: saved, identify: () => 'instant-'+(++sequence)});
  const init = {method: 'POST', body: '{"id":"FAST","background":true}'};
  await fetch('/api/run-case', init);
  await fetch('/api/run-case', init);
  assert.equal(ids[0], ids[1]);
  await fetch.completeExecution('instant-job');
  await fetch('/api/run-case', init);
  assert.notEqual(ids[0], ids[2]);
});

test('execution acknowledgement preserves other pending jobs and rejects invalid identifiers', async () => {
  const saved = storage(), ids = [];
  let sequence = 0;
  const create = () => api.createFetch(async (_url, init) => {
    const body = JSON.parse(init.body);
    ids.push(body.requestId);
    return response({ok: true, data: {pending: true, executionId: 'job-'+body.id}});
  }, {storage: saved, identify: () => 'concurrent-'+(++sequence)});
  const first = create();
  const a = {method: 'POST', body: '{"id":"A","background":true}'};
  const b = {method: 'POST', body: '{"id":"B","background":true}'};
  await first('/api/run-case', a);
  await first('/api/run-case', b);
  const restored = create();
  for (const invalid of [undefined, null, {}, ['job-B'], '', ' job-B', 'job-']) {
    await restored.completeExecution(invalid);
  }
  await restored.completeExecution('job-A');
  await restored('/api/run-case', b);
  await restored('/api/run-case', a);
  assert.equal(ids[1], ids[2]);
  assert.notEqual(ids[0], ids[3]);
});

test('query errors or unknown states retain the original request across reload', async () => {
  const saved = storage(), ids = [];
  let sequence = 0;
  const create = () => api.createFetch(async (url, init) => {
    if (url.includes('/api/execution/job')) return response({ok: true, data: {status: 'unknown'}});
    ids.push(JSON.parse(init.body).requestId);
    return response({ok: true, data: {pending: true, executionId: 'unconfirmed-job'}});
  }, {storage: saved, identify: () => 'unknown-'+(++sequence)});
  const first = create();
  const init = {method: 'POST', body: '{"id":"UNKNOWN","background":true}'};
  await first('/api/run-case', init);
  await assert.rejects(api.pollJob(first, '/api/execution/job?id=unconfirmed-job'), /无法识别/);
  const restored = create();
  await restored('/api/run-case', init);
  assert.equal(ids[0], ids[1]);
});

test('an unrecognized submission response retains the request ID for manual retry', async () => {
  const saved = storage(), ids = [];
  let sequence = 0;
  const create = () => api.createFetch(async (_url, init) => {
    ids.push(JSON.parse(init.body).requestId);
    return response({unexpected: 'response'});
  }, {storage: saved, identify: () => 'unrecognized-'+(++sequence)});
  const init = {method: 'POST', body: '{"id":"UNKNOWN","background":true}'};
  await create()('/api/run-case', init);
  await create()('/api/run-case', init);
  assert.equal(ids[0], ids[1]);
});

test('a delayed response from an acknowledged execution cannot replace the next request mapping', async () => {
  const saved = storage(), ids = [];
  let sequence = 0, resolveLate;
  const fetch = api.createFetch(async (_url, init) => {
    ids.push(JSON.parse(init.body).requestId);
    if (ids.length === 2) return new Promise(resolve => { resolveLate = resolve; });
    return response({ok: true, data: {pending: true, executionId: 'job-'+ids.at(-1)}});
  }, {storage: saved, identify: () => 'ordered-'+(++sequence)});
  const init = {method: 'POST', body: '{"id":"ORDERED","background":true}'};
  await fetch('/api/run-case', init);
  const late = fetch('/api/run-case', init);
  const deadline = Date.now()+2000;
  while (!resolveLate) {
    assert.ok(Date.now() < deadline, 'the duplicate request did not start');
    await new Promise(resolve => setImmediate(resolve));
  }
  await fetch.completeExecution('job-'+ids[0]);
  await fetch('/api/run-case', init);
  resolveLate(response({ok: true, data: {pending: true, executionId: 'job-'+ids[0]}}));
  await late;
  const restored = api.createFetch(async (_url, options) => {
    ids.push(JSON.parse(options.body).requestId);
    return response({ok: true});
  }, {storage: saved, identify: () => 'unexpected-new-request'});
  await restored('/api/run-case', init);
  assert.notEqual(ids[0], ids[2]);
  assert.equal(ids[2], ids[3]);
});

test('memory-only execution acknowledgement never persists request parameters', async () => {
  const originalCrypto = globalThis.crypto;
  globalThis.crypto = undefined;
  try {
    const saved = storage(), ids = [];
    let sequence = 0;
    const fetch = api.createFetch(async (_url, init) => {
      ids.push(JSON.parse(init.body).requestId);
      return response({ok: true, data: {pending: true, executionId: 'memory-job'}});
    }, {storage: saved, identify: () => 'memory-'+(++sequence)});
    const init = {method: 'POST', body: '{"password":"SYNTHETIC_SECRET","background":true}'};
    await fetch('/api/run-case', init);
    await fetch.completeExecution('memory-job');
    await fetch('/api/run-case', init);
    assert.notEqual(ids[0], ids[1]);
    assert.doesNotMatch([...saved.records.values()].join(''), /SYNTHETIC_SECRET|password/);
  } finally { globalThis.crypto = originalCrypto; }
});

test('an expired stored result is displayed as incomplete and asks for business-state verification', async () => {
  const source = panel.slice(panel.indexOf('async function watchActiveJob(){'), panel.indexOf('async function runCase('));
  const texts = {}, nodes = new Map(), statuses = [];
  let completed, rendered;
  const run = new AsyncFunction('activeRun', 'watching', '$', 'renderCases', 'PLATFORMExecution', 'fetch', 'setText', 'renderResult', 'showStatus', 'saveActiveJob',
    source+'\nawait watchActiveJob();');
  await run({jobId: 'expired-job', caseId: 'AUDIT-L01'}, false,
    id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); }, () => {},
    {pollJob: async () => ({status: 'incomplete', resultExpired: true})},
    {completeExecution: async id => { completed = id; }},
    (id, value) => { texts[id] = value; }, data => { rendered = data; },
    (...args) => { statuses.push(args); }, () => {});
  assert.equal(completed, 'expired-job');
  assert.equal(texts['result-status'], 'incomplete');
  assert.equal(rendered.ok, null);
  assert.equal(rendered.resultExpired, true);
  assert.match(rendered.error, /核验业务状态/);
  assert.deepEqual(statuses, [['执行结果已过期 · 请先核验业务状态', true]]);
});

const batchSource = panel.slice(panel.indexOf('function prepareBatchRequest('), panel.indexOf("$('only-failed').onclick"));

test('SAFE batch confirmation follows selected SAFE write flags and cancellation prevents submission', async () => {
  for (const flags of [{}, {requires_write_opt_in: true}, {requires_unscoped_write_opt_in: true}]) {
    for (const approved of [false, true]) {
      const confirmations = [], bodies = [];
      const context = vm.createContext({
        TestDockSecurity: require('../assets/security-client.js'), activeRun: null, watching: false, selectedSuite: 'AUDIT',
        allCases: [{id: 'AUDIT-R01', suite: 'AUDIT', mode: 'safe', ...flags},
          {id: 'AUDIT-L01', suite: 'AUDIT', mode: 'live', requires_write_opt_in: true},
          {id: 'OTHER-R01', suite: 'OTHER', mode: 'safe', requires_unscoped_write_opt_in: true}],
        confirm: message => { confirmations.push(message); return approved; },
        api: async (_url, init) => { bodies.push(JSON.parse(init.body)); return {executionId: 'synthetic-job'}; },
        renderCases(){}, setText(){}, saveActiveJob(){}, watchActiveJob: async () => {}, showStatus(){}
      });
      vm.runInContext(batchSource, context);
      await context.runBatch('safe');
      const writes = Boolean(flags.requires_write_opt_in || flags.requires_unscoped_write_opt_in);
      assert.equal(confirmations.length, writes ? 1 : 0);
      if (writes) assert.match(confirmations[0], /SAFE.*写入.*AUDIT/);
      if (writes && !approved) assert.equal(bodies.length, 0);
      else {
        assert.equal(bodies.length, 1);
        assert.equal(bodies[0].mode, 'safe');
        assert.equal(bodies[0].suite, 'AUDIT');
        assert.equal(bodies[0].confirmLive, writes ? 'AUDIT' : undefined);
      }
    }
  }
});

test('LIVE batch still requires explicit confirmation and preserves its suite acknowledgement', async () => {
  for (const approved of [false, true]) {
    const confirmations = [], bodies = [];
    const context = vm.createContext({
      TestDockSecurity: require('../assets/security-client.js'), activeRun: null, watching: false, selectedSuite: 'AUDIT',
      allCases: [{id: 'AUDIT-R01', suite: 'AUDIT', mode: 'safe'}],
      confirm: message => { confirmations.push(message); return approved; },
      api: async (_url, init) => { bodies.push(JSON.parse(init.body)); return {executionId: 'live-job'}; },
      renderCases(){}, setText(){}, saveActiveJob(){}, watchActiveJob: async () => {}, showStatus(){}
    });
    vm.runInContext(batchSource, context);
    await context.runBatch('live');
    assert.equal(confirmations.length, 1);
    assert.equal(bodies.length, approved ? 1 : 0);
    if (approved) assert.equal(bodies[0].confirmLive, 'AUDIT');
  }
});

test('confirmed SAFE write batches release their original request IDs after terminal acknowledgement', async () => {
  const saved = storage(), ids = [];
  let sequence = 0;
  const fetch = api.createFetch(async (_url, init) => {
    const body = JSON.parse(init.body);
    assert.equal(body.confirmLive, 'AUDIT');
    ids.push(body.requestId);
    return response({ok: true, data: {pending: true, executionId: 'safe-write-job-'+body.requestId}});
  }, {storage: saved, identify: () => 'safe-write-'+(++sequence)});
  const context = vm.createContext({
    TestDockSecurity: require('../assets/security-client.js'), activeRun: null, watching: false, selectedSuite: 'AUDIT',
    allCases: [{id: 'AUDIT-R01', suite: 'AUDIT', mode: 'safe', requires_write_opt_in: true}],
    confirm: () => true,
    api: async (url, init) => (await (await fetch(url, init)).json()).data,
    renderCases(){}, setText(){}, saveActiveJob(){}, showStatus(){},
    watchActiveJob: async () => { await finishFromPanel(fetch, context.activeRun); context.activeRun = null; }
  });
  vm.runInContext(batchSource, context);
  await context.runBatch('safe');
  await context.runBatch('safe');
  assert.equal(ids.length, 2);
  assert.notEqual(ids[0], ids[1]);
});

test('batch submission blocks another click and retains the submitted suite while navigation changes', async () => {
  const bodies = [];
  let resolveSubmission, watched;
  const context = vm.createContext({
    TestDockSecurity: require('../assets/security-client.js'), activeRun: null, watching: false, selectedSuite: 'AUDIT',
    allCases: [{id: 'AUDIT-R01', suite: 'AUDIT', mode: 'safe'}], confirm: () => true,
    api: (_url, init) => { bodies.push(JSON.parse(init.body)); return new Promise(resolve => { resolveSubmission = resolve; }); },
    renderCases(){}, setText(){}, saveActiveJob(){}, showStatus(){},
    watchActiveJob: async () => { watched = context.activeRun; }
  });
  vm.runInContext(batchSource, context);
  const pending = context.runBatch('safe');
  assert.equal(context.watching, true);
  context.selectedSuite = 'OTHER';
  await context.runBatch('safe');
  assert.equal(bodies.length, 1);
  resolveSubmission({executionId: 'submitted-job'});
  await pending;
  assert.equal(watched.caseId, 'AUDIT');
});
