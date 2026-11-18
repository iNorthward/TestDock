const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function setup(stored, denied=false) {
  const values = new Map(stored ? [['platform-ui-theme', stored]] : []);
  const attributes={}; const events=[];
  const context={localStorage:{getItem:key=>{if(denied)throw Error('private');return values.get(key);},setItem:(key,value)=>{if(denied)throw Error('private');values.set(key,value);}},
    document:{readyState:'loading',documentElement:{setAttribute:(key,value)=>attributes[key]=value},
      createElement:()=>({}),head:{appendChild:()=>{}},querySelectorAll:()=>[],
      addEventListener:()=>{},dispatchEvent:event=>events.push(event)},
    CustomEvent:class{constructor(name,options){this.type=name;this.detail=options.detail;}}};
  context.window=context; vm.createContext(context);
  for(const name of ['theme-boot.js','theme-switcher.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets',name),'utf8'),context);
  return {context,values,attributes,events};
}
test('invalid persisted theme returns to a visible default',()=>{
  const state=setup('unknown-theme');assert.equal(state.attributes['data-theme'],'claude');assert.equal(state.values.get('platform-ui-theme'),'claude');
});
test('all four visible themes persist and emit a selection event',()=>{
  const state=setup();
  for(const theme of ['claude','night','studio','console']){
    state.context.PLATFORMTheme.setTheme(theme);assert.equal(state.context.PLATFORMTheme.getTheme(),theme);
    assert.equal(state.attributes['data-theme'],theme);assert.equal(state.events.at(-1).detail.theme,theme);
  }
  assert.equal(state.context.PLATFORMTheme.THEMES.length,4);
});
test('private storage does not stop theme rendering',()=>{
  const state=setup(null,true);assert.equal(state.attributes['data-theme'],'claude');state.context.PLATFORMTheme.setTheme('night');assert.equal(state.attributes['data-theme'],'night');
});

function workspace(stored='[]',denied=false) {
  const events={},properties={},values=new Map([['testdock.workspace.tabs',stored]]);
  const node=()=>({children:[],style:{},append(...items){this.children.push(...items);},
    replaceChildren(){this.children=[];},setAttribute(key,value){this[key]=String(value);},
    addEventListener(key,handler){this[key]=handler;}});
  const tabs=node(),body=node();body.classList={add(){},remove(){}};
  const storage={getItem:key=>{if(denied)throw Error('private storage');return values.get(key)||null;},
    setItem:(key,value)=>{if(denied)throw Error('private storage');values.set(key,value);}};
  const context={location:new URL('http://localhost:8801/'),URL,sessionStorage:storage,localStorage:storage,
    document:{getElementById:()=>tabs,querySelector:()=>null,createElement:node,body,
      documentElement:{style:{setProperty:(key,value)=>properties[key]=value}}},
    addEventListener:(key,handler)=>events[key]=handler,setTimeout:handler=>handler(),scrollTo(){},scrollY:12};
  context.window=context;vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../assets/workspace-shell.js'),'utf8'),context);
  return {context,events,properties,values,tabs,body};
}
test('workspace recovers malformed storage and rejects unsafe cached tab links',()=>{
  for(const stored of ['null','{}','"text"','[null,12,{"url":"javascript:alert(1)","title":"unsafe"}]']) {
    const state=workspace(stored);
    assert.equal(state.tabs.children.length,1);
    assert.equal(state.tabs.children[0].children[0].href,'http://localhost:8801/');
    assert.equal(state.properties['--sidebar-w'],'260px');
  }
});
test('workspace remains usable when browser storage is denied',()=>{
  const state=workspace('[]',true);
  state.context.WorkspaceShell.open('http://localhost:8801/whitepaper','平台白皮书');
  assert.equal(state.tabs.children.length,2);
  state.events.pagehide();state.events.load();
  assert.equal(state.properties['--sidebar-w'],'260px');
});

test('UI scenes share a workspace tab across filters and close from a filtered URL', () => {
  const state = workspace();
  const base = 'http://localhost:8801/ui-scenes';
  state.context.location = new URL(base + '?scene=A01');
  state.context.WorkspaceShell.open(state.context.location.href, 'UI 场景选型');
  state.context.location = new URL(base + '?page=review&scene=E10');
  state.context.WorkspaceShell.open(state.context.location.href, 'UI 场景选型');
  assert.equal(state.tabs.children.length, 2);
  assert.equal(state.tabs.children[1].children[0].href, state.context.location.href);
  assert.match(state.tabs.children[1].className, / on$/);
  state.context.location = new URL(base + '?page=reports&scene=F06');
  state.events.pagehide();
  assert.equal(JSON.parse(state.values.get('testdock.workspace.tabs'))[1].url, state.context.location.href);
  state.tabs.children[1].children[1].onclick();
  assert.equal(state.context.location.href, 'http://localhost:8801/');
});
