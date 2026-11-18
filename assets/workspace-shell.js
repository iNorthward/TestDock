(()=>{
 const scrollKey='testdock.workspace.scroll', key='testdock.workspace.tabs', widthKey='testdock.sidebar.width';
 function read(storage,key,fallback){
  try{return window[storage].getItem(key)||fallback;}catch(_){return fallback;}
 }
 function write(storage,key,value){
  try{window[storage].setItem(key,value);}catch(_){}
 }
 function tabUrl(value){
  if(typeof value!=='string')return null;
  try{
   const url=new URL(value,location.origin);
   return url.origin===location.origin && ['http:','https:'].includes(url.protocol)
     && !url.username && !url.password ? url.href : null;
  }catch(_){return null;}
 }
 function tabKey(url){
  const parsed=new URL(url);
  return parsed.pathname==='/ui-scenes' ? parsed.origin+parsed.pathname : parsed.href;
 }
 let tabs=[];
 try{
  const stored=JSON.parse(read('sessionStorage',key,'[]'));
  if(Array.isArray(stored))for(const item of stored){
   if(!item || typeof item.title!=='string')continue;
   const url=tabUrl(item.url);
   if(url && !tabs.some(tab=>tabKey(tab.url)===tabKey(url)))tabs.push({url,title:item.title});
  }
 }catch(_){}
 function render(){
  const root=document.getElementById('workspace-tabs');if(!root)return;
  root.replaceChildren();
  for(const tab of tabs){
   const current=()=>tabKey(tab.url)===tabKey(location.href);
   const wrap=document.createElement('div');wrap.className='workspace-tab'+(current()?' on':'');
   const link=document.createElement('a');link.href=tab.url;link.textContent=tab.title;
   const close=document.createElement('button');close.className='wt-close';close.textContent='×';close.setAttribute('aria-label','关闭 '+tab.title);
   close.onclick=()=>{tabs=tabs.filter(x=>x!==tab);save();if(current())location.href=tabs.at(-1)?.url||'/#/home';};
   wrap.append(link,close);root.append(wrap);
  }
 }
 function positions(){
  try{const value=JSON.parse(read('sessionStorage',scrollKey,'{}'));if(value && typeof value==='object' && !Array.isArray(value))return value;}catch(_){}
  return {};
 }
 window.addEventListener('pagehide',()=>{
  const stored=positions();stored[location.href]=window.scrollY;write('sessionStorage',scrollKey,JSON.stringify(stored));
  if(location.pathname==='/ui-scenes'){
   const tab=tabs.find(item=>tabKey(item.url)===tabKey(location.href));
   if(tab){tab.url=location.href;write('sessionStorage',key,JSON.stringify(tabs));}
  }
 });
 window.addEventListener('load',()=>{
  const value=positions()[location.href];
  if(location.pathname==='/ui-scenes'||location.pathname==='/ui-scenes/demo'){
   if(Number.isFinite(value)&&value>0)window.scrollTo(0,value);
   return;
  }
  setTimeout(()=>window.scrollTo(0,typeof value==='number' && Number.isFinite(value) && value>=0?value:0),100);
 });
 function save(){write('sessionStorage',key,JSON.stringify(tabs));render();}
 window.WorkspaceShell={open(value,title){
  const url=tabUrl(value);if(!url || typeof title!=='string')return;
  const tab=tabs.find(x=>tabKey(x.url)===tabKey(url));if(tab)Object.assign(tab,{url,title});else tabs.push({url,title});save();
 }};
 if(location.pathname!=='/')window.WorkspaceShell.open(location.href,document.body.dataset?.workspaceTitle||
  (location.pathname==='/whitepaper'?'平台白皮书':location.pathname==='/insights'?'测试洞察':'平台文档'));
 else window.WorkspaceShell.open(location.href,window.WorkspaceTitle||(tabs.find(x=>x.url===location.href)?.title)||(location.hash.startsWith('#/cases')?'测试用例':'测试用例总览'));
 window.addEventListener('document-loaded',event=>window.WorkspaceShell.open(location.href,event.detail));
 if(location.pathname!=='/'){
  const sidebar=document.querySelector('.sidebar');
  if(sidebar){fetch('/api/catalog?lite=1').then(r=>r.json()).then(payload=>{
   const brand=sidebar.querySelector('.brand'),uiScenes=sidebar.querySelector('a[href="/ui-scenes"]');
   sidebar.replaceChildren();if(brand)sidebar.append(brand);
   const addLink=(label,url)=>{const a=document.createElement('a');a.className='nav-item'+(new URL(url,location.origin).href===location.href?' active':'');a.href=url;a.textContent=label;sidebar.append(a);};
   const label=text=>{const div=document.createElement('div');div.className='nav-label';div.textContent=text;sidebar.append(div);};
   addLink('🏠 首页','/#/home');
   if(uiScenes)sidebar.append(uiScenes);else addLink('UI 场景选型','/ui-scenes');
   label('测试用例 · '+(payload.data?.cases||[]).length+' 条');
   const groups=new Map();for(const suite of payload.data?.suites||[]){const name=suite.parent||'测试用例';if(!groups.has(name))groups.set(name,[]);groups.get(name).push(suite);}
   for(const [name,suites] of groups){
    const group=document.createElement('details');group.className='nav-group';group.open=read('sessionStorage','testdock.nav.'+name,'')!=='closed';
    group.ontoggle=()=>write('sessionStorage','testdock.nav.'+name,group.open?'open':'closed');
    const summary=document.createElement('summary');summary.textContent=name;group.append(summary);
    for(const suite of suites){const link=document.createElement('a');link.className='nav-item suite-link';link.href='/#/cases/'+encodeURIComponent(suite.id);link.textContent=suite.name;group.append(link);}
    sidebar.append(group);
   }
   label('文档');addLink('📖 平台白皮书','/whitepaper');
  }).catch(()=>{});}
 }
 const resizer=document.createElement('div');resizer.className='sidebar-resizer';resizer.setAttribute('role','separator');resizer.setAttribute('aria-label','调整侧栏宽度');resizer.setAttribute('aria-orientation','vertical');resizer.setAttribute('aria-valuemin','200');resizer.setAttribute('aria-valuemax','480');resizer.tabIndex=0;(document.querySelector('main')||document.body).append(resizer);
 function width(value){
  const bounded=Math.max(200,Math.min(480,Number.isFinite(value)?value:260));
  document.documentElement.style.setProperty('--sidebar-w',bounded+'px');resizer.setAttribute('aria-valuenow',bounded);write('localStorage',widthKey,String(bounded));
 }
 width(Number(read('localStorage',widthKey,'260'))||260);
 resizer.addEventListener('pointerdown',event=>{resizer.setPointerCapture(event.pointerId);document.body.classList.add('sidebar-resizing');});
 resizer.addEventListener('pointermove',event=>{if(resizer.hasPointerCapture(event.pointerId))width(event.clientX);});
 resizer.addEventListener('pointerup',()=>document.body.classList.remove('sidebar-resizing'));
 resizer.addEventListener('pointercancel',()=>document.body.classList.remove('sidebar-resizing'));
 resizer.addEventListener('dblclick',()=>width(260));
 resizer.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();width(parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'))+(event.key==='ArrowLeft'?-10:10));}});
})();
