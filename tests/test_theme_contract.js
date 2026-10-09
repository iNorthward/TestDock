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
