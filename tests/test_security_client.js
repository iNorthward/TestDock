const test = require('node:test');
const assert = require('node:assert/strict');
const {createFetch} = require('../assets/security-client.js');

test('session is shared by concurrent requests and token only sent to own API', async () => {
  const calls = [];
  const original = async (url, init = {}) => {
    calls.push([url, init]);
    return {ok:true, json:async () => ({token:'synthetic-session'})};
  };
  const fetch = createFetch(original, {origin:'http://localhost:8801'});
  await Promise.all([fetch('/api/catalog'), fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})]);
  assert.equal(calls.filter(([url]) => url === '/api/session').length,1);
  for(const [url, init] of calls.filter(([url]) => url !== '/api/session')) {
    assert.equal(init.headers.get('X-TestDock-Token'),'synthetic-session');
    if(url === '/api/run') assert.equal(init.headers.get('Content-Type'),'application/json');
  }
  await fetch('https://external.example/api/run');
  assert.equal(calls.at(-1)[1].headers,undefined);
});

test('failed session initialization blocks dispatch and may be retried', async () => {
  let available=false, dispatched=0;
  const fetch=createFetch(async url => {
    if(url==='/api/session') return {ok:available,json:async()=>available?{token:'new-session'}:{}};
    dispatched++;return {};
  },{origin:'http://localhost:8801'});
  await assert.rejects(fetch('/api/catalog'));
  assert.equal(dispatched,0);
  available=true;await fetch('/api/catalog');assert.equal(dispatched,1);
});

test('POST SSE parses frames split across chunks and delivers terminal completion', async () => {
  const {postEvents} = require('../assets/security-client.js');
  const previous = global.fetch;
  const encoder = new TextEncoder();
  let submitted;
  global.fetch = async (url,init) => {
    submitted = init;
    return {ok:true,body:new ReadableStream({start(controller){
      controller.enqueue(encoder.encode('data: {"type":"pro'));
      controller.enqueue(encoder.encode('gress","current":1}\n\ndata: {"type":"complete","result":{"ok":true}}\n\n'));
      controller.close();
    }})};
  };
  try {
    const events=[];
    await new Promise((resolve,reject) => {
      const stream=postEvents('/api/db-schema/patrol-stream');
      stream.onerror=reject;
      stream.onmessage=event => {const data=JSON.parse(event.data);events.push(data.type);if(data.type==='complete')resolve();};
    });
    assert.equal(submitted.method,'POST');
    assert.equal(submitted.headers['Content-Type'],'application/json');
    assert.deepEqual(events,['progress','complete']);
  } finally {global.fetch=previous;}
});

test('POST SSE reports server rejection without claiming completion', async () => {
  const {postEvents}=require('../assets/security-client.js');
  const previous=global.fetch;
  global.fetch=async()=>({ok:false,json:async()=>({error:'capability unavailable'})});
  try {
    const error=await new Promise(resolve=>{
      const stream=postEvents('/api/db-schema/patrol-stream');
      stream.onerror=resolve;
      stream.onmessage=()=>assert.fail('unexpected progress');
    });
    assert.match(error.message,/capability unavailable/);
  } finally {global.fetch=previous;}
});

test('error log GET stream sends session header and releases the reader', async () => {
  const {getEvents} = require('../assets/security-client.js');
  const previous = global.fetch;
  const calls = [];
  const encoder = new TextEncoder();
  let cancelled = false;
  global.fetch = createFetch(async (url, init = {}) => {
    calls.push([url, init]);
    if (url === '/api/session') return {ok:true, json:async () => ({token:'synthetic-stream-token'})};
    return {ok:true, body:new ReadableStream({start(controller) {
      controller.enqueue(encoder.encode('data: {"type":"progress","percent":10}\n\n'));
      controller.enqueue(encoder.encode('data: {"type":"complete","result":{"ok":true}}\n\n'));
    }, cancel() { cancelled = true; }})};
  }, {origin:'http://localhost:8801'});
  try {
    const kinds = [];
    await new Promise((resolve,reject) => {
      const stream = getEvents('/api/error-log/run-stream?env=synthetic&from=2026-10-10&to=2026-10-11');
      stream.onerror = reject;
      stream.onmessage = event => {
        const data = JSON.parse(event.data);
        kinds.push(data.type);
        if (data.type === 'complete') {stream.close(); resolve();}
      };
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(kinds, ['progress', 'complete']);
    assert.equal(calls.length, 2);
    assert.equal(calls[1][1].method, 'GET');
    assert.equal(calls[1][1].headers.get('X-TestDock-Token'), 'synthetic-stream-token');
    assert.equal(cancelled, true);
    const page = require('node:fs').readFileSync(require('node:path').join(__dirname,'../test-platform/insights.html'),'utf8');
    assert.doesNotMatch(page, /new\s+EventSource\s*\(/);
    assert.match(page, /errorLogEs=TestDockSecurity\.getEvents\(url\)/);
  } finally { global.fetch = previous; }
});

test('panel refuses cancelled confirmation for every write flag and only sends confirmed LIVE ids', async () => {
  const vm = require('node:vm');
  const page = require('node:fs').readFileSync(require('node:path').join(__dirname,'../test-platform/panel.html'),'utf8');
  const source = page.slice(page.indexOf('async function runCase('), page.indexOf("$('only-failed').onclick"));
  for (const flags of [{mode:'live'}, {mode:'safe',requires_write_opt_in:true}, {mode:'safe',requires_unscoped_write_opt_in:true}, {mode:'safe'}]) {
    for (const approved of [false,true]) {
      const calls=[], confirmations=[];
      const context = vm.createContext({
        TestDockSecurity:require('../assets/security-client.js'), activeRun:null, watching:false,
        confirm:message => {confirmations.push(message); return approved;},
        api:async (url,init) => {calls.push(JSON.parse(init.body)); return {executionId:'synthetic-job'};},
        renderCases(){},setText(){},saveActiveJob(){},watchActiveJob:async()=>{},showStatus(){}
      });
      vm.runInContext(source, context);
      const writes = flags.mode === 'live' || flags.requires_write_opt_in || flags.requires_unscoped_write_opt_in;
      await context.runCase({id:'SYNTHETIC-R01',...flags}, {disabled:false});
      assert.equal(confirmations.length,writes?1:0);
      if (writes && !approved) assert.equal(calls.length,0);
      else {
        assert.equal(calls.length,1);
        assert.equal(calls[0].confirmLive,writes?'SYNTHETIC-R01':undefined);
      }
    }
  }
});

test('panel shows pending reload and refreshes after the catalog revision advances', async () => {
  const vm = require('node:vm');
  const page = require('node:fs').readFileSync(require('node:path').join(__dirname,'../test-platform/panel.html'),'utf8');
  const source = page.slice(page.indexOf('async function pollCatalogRevision('),page.indexOf('async function loadCapabilities('));
  const badge = {hidden:true};
  const replies = [{revision:7,reloadPending:true},{revision:8,reloadPending:false}];
  let refreshes = 0;
  const context = vm.createContext({
    catalogRevision:7, revisionChecking:false, console,
    $:id => {assert.equal(id,'catalog-pending'); return badge;},
    api:async url => {assert.equal(url,'/api/catalog/revision');return replies.shift();},
    loadCatalog:async() => {refreshes++;}
  });
  vm.runInContext(source,context);
  await context.pollCatalogRevision();
  assert.equal(badge.hidden,false);
  assert.equal(refreshes,0);
  await context.pollCatalogRevision();
  assert.equal(badge.hidden,true);
  assert.equal(refreshes,1);
  assert.equal(context.catalogRevision,8);
  assert.match(page,/setInterval\(pollCatalogRevision,1500\)/);
});
