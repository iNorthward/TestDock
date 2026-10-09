(()=>{
 const scrollKey='testdock.workspace.scroll';const key='testdock.workspace.tabs',widthKey='testdock.sidebar.width';let tabs=[];
 try{tabs=JSON.parse(sessionStorage.getItem(key)||'[]');}catch(_){}
 function render(){const root=document.getElementById('workspace-tabs');if(!root)return;root.replaceChildren();for(const tab of tabs){const wrap=document.createElement('div');wrap.className='workspace-tab'+(tab.url===location.href?' on':'');const link=document.createElement('a');link.href=tab.url;link.textContent=tab.title;const close=document.createElement('button');close.className='wt-close';close.textContent='×';close.setAttribute('aria-label','关闭 '+tab.title);close.onclick=()=>{tabs=tabs.filter(x=>x.url!==tab.url);save();if(tab.url===location.href)location.href=tabs.at(-1)?.url||'/#/home';};wrap.append(link,close);root.append(wrap);}}
 window.addEventListener('pagehide',()=>{let positions={};try{positions=JSON.parse(sessionStorage.getItem(scrollKey)||'{}');}catch(_){}positions[location.href]=window.scrollY;sessionStorage.setItem(scrollKey,JSON.stringify(positions));});
 window.addEventListener('load',()=>{try{const positions=JSON.parse(sessionStorage.getItem(scrollKey)||'{}');setTimeout(()=>window.scrollTo(0,positions[location.href]||0),100);}catch(_){}});
 function save(){sessionStorage.setItem(key,JSON.stringify(tabs));render();}
 window.WorkspaceShell={open(url,title){const tab=tabs.find(x=>x.url===url);if(tab)tab.title=title;else tabs.push({url,title});save();}};
 if(location.pathname!=='/')window.WorkspaceShell.open(location.href,location.pathname==='/whitepaper'?'平台白皮书':location.pathname==='/insights'?'测试洞察':'平台文档');else window.WorkspaceShell.open(location.href,window.WorkspaceTitle||(tabs.find(x=>x.url===location.href)?.title)||(location.hash.startsWith('#/cases')?'测试用例':'测试用例总览'));
 window.addEventListener('document-loaded',event=>window.WorkspaceShell.open(location.href,event.detail));
 if(location.pathname!=='/'){
 const sidebar=document.querySelector('.sidebar');
 if(sidebar){fetch('/api/catalog?lite=1').then(r=>r.json()).then(payload=>{
  const brand=sidebar.querySelector('.brand');sidebar.replaceChildren();if(brand)sidebar.append(brand);
  const addLink=(label,url)=>{const a=document.createElement('a');a.className='nav-item'+(new URL(url,location.origin).href===location.href?' active':'');a.href=url;a.textContent=label;sidebar.append(a);};const label=text=>{const div=document.createElement('div');div.className='nav-label';div.textContent=text;sidebar.append(div);};
  addLink('🏠 首页','/#/home');label('测试用例 · '+(payload.data?.cases||[]).length+' 条');
  const groups=new Map();for(const suite of payload.data?.suites||[]){const name=suite.parent||'测试用例';if(!groups.has(name))groups.set(name,[]);groups.get(name).push(suite);}
  for(const [name,suites] of groups){const group=document.createElement('details');group.className='nav-group';group.open=sessionStorage.getItem('testdock.nav.'+name)!=='closed';group.ontoggle=()=>sessionStorage.setItem('testdock.nav.'+name,group.open?'open':'closed');const summary=document.createElement('summary');summary.textContent=name;group.append(summary);for(const suite of suites){const link=document.createElement('a');link.className='nav-item suite-link';link.href='/#/cases/'+encodeURIComponent(suite.id);link.textContent=suite.name;group.append(link);}sidebar.append(group);}
  label('文档');addLink('📖 平台白皮书','/whitepaper');
 }).catch(()=>{});}
}
 const resizer=document.createElement('div');resizer.className='sidebar-resizer';resizer.setAttribute('role','separator');resizer.setAttribute('aria-label','调整侧栏宽度');resizer.tabIndex=0;document.body.append(resizer);
 function width(value){document.documentElement.style.setProperty('--sidebar-w',Math.max(200,Math.min(480,value))+'px');localStorage.setItem(widthKey,String(value));}
 width(Number(localStorage.getItem(widthKey))||260);
 resizer.addEventListener('pointerdown',event=>{resizer.setPointerCapture(event.pointerId);document.body.classList.add('sidebar-resizing');});
 resizer.addEventListener('pointermove',event=>{if(resizer.hasPointerCapture(event.pointerId))width(event.clientX);});
 resizer.addEventListener('pointerup',()=>document.body.classList.remove('sidebar-resizing'));
 resizer.addEventListener('dblclick',()=>width(260));resizer.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();width(parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'))+(event.key==='ArrowLeft'?-10:10));}});
})();
