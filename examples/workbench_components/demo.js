(function () {
  'use strict';
  const ui = window.PlatformWorkbench, $ = id => document.getElementById(id), mode = document.body.dataset.example;
  const config = mode === 'inventory' ? {
    pack:'demo_inventory', page:'observations', timeZone:'Asia/Shanghai', names:['inventory-note','inventory-kind','inventory-count'],
    choices:[['boxes','收纳用品'],['tools','手工工具']], title:'合成观察', kind:'观察类别', exact:'900719925474099312345.1234567890123456789000', trim:true,
  } : {
    pack:'demo_schedule', page:'agenda', timeZone:'UTC', names:['schedule-title','schedule-layout','schedule-seats'],
    choices:[['circle','围坐讨论'],['classroom','课堂布局']], title:'合成活动', kind:'空间布置', exact:'000900719925474099312345', trim:false,
  };
  const later = ms => new Promise(resolve => setTimeout(resolve, ms));
  const formStatus = ui.feedback($('form-status')), listStatus = ui.feedback($('list-status')), taskStatus = ui.feedback($('task-status'));
  let safeStorage;
  try {safeStorage = window.sessionStorage;} catch (_) {formStatus.set('浏览器存储不可用，刷新后无法恢复。',{kind:'warning'});}
  const fields = Object.fromEntries(config.names.map(name => [name, $(name)]));
  const context = ui.createFormContext({pack:config.pack,page:config.page,scope:$('scope').value,version:1,fields,storage:safeStorage,onNotice:message=>formStatus.set(message,{kind:'warning'})});
  let optionGeneration = 0;
  function loadOptions(saved) {
    const generation = ++optionGeneration, node = fields[config.names[1]];
    node.replaceChildren(new Option('选项加载中…', '')); context.setPending([config.names[1]]);
    formStatus.set('选项正在加载；输入内容会即时保存。');
    setTimeout(() => {
      if (generation !== optionGeneration) return;
      node.replaceChildren(new Option('请选择', ''), ...config.choices.map(([value,label])=>new Option(label,value)));
      const value=saved[config.names[1]]; if (typeof value === 'string' && config.choices.some(choice=>choice[0]===value)) node.value=value;
      context.setPending([]); context.save(); formStatus.set('当前作用域 '+$('scope').value+'；白名单字段已即时保存。',{kind:'success'});
    }, 1100);
  }
  loadOptions(context.restore()); context.bind();
  $('scope').addEventListener('change',()=>{loadOptions(context.setScope($('scope').value));list.query({filters:{search:$('search').value},limit:$('limit').value});});
  ui.presentExact($('exact-value'),config.exact,{trimFractionZeros:config.trim});
  $('copy').addEventListener('click',()=>ui.copyExact(config.exact,{status:$('copy-status')}));

  const details=ui.createDetailsDialog({dialog:$('details'),body:document.body,status:$('details-status'),content:$('details-content'),project:value=>[
    '合成记录：'+value.reference, config.kind+'：'+value.kind, '原始文本：'+value.exact, '业务状态：'+value.businessStatus,
  ].join('\n')});
  $('close-details').addEventListener('click',details.close);$('retry-details').addEventListener('click',details.retry);
  function openDetails(row,trigger) {
    const initial={reference:row.key,kind:row.kind,exact:row.exact,businessStatus:'仍待页面规则判断',privateSyntheticField:'此字段没有被允许展示'};
    details.open(trigger,{key:$('scope').value+':'+row.key,initial,load:async()=>{await later(220);if($('details-failure').checked)throw new Error('合成只读接口暂时不可用');return initial;}});
  }
  const cursors = new Map([['segment:3+/==',3],['segment:5+/==',5],['segment:6+/==',6],['segment:8+/==',8]]);
  const list=ui.createReadonlyList({
    scope:()=>$('scope').value, keyOf:row=>row.key, rowsOf:data=>data.entries,cursorOf:data=>data.continueAt,
    async load({filters,limit,cursor,scope}) {
      // Synthetic transport intentionally ignores AbortSignal so stale-response protection is visible.
      await later(filters.search==='slow'?1400:160);
      const search=filters.search.trim(),start=cursor===null?0:cursors.get(cursor);
      if(start===undefined)throw new Error('未知合成游标');
      const all=Array.from({length:9},(_,index)=>({key:config.pack+'-'+scope+'-'+(index+1),title:config.title+' '+(index+1)+(search?' · '+search:''),kind:config.choices[index%2][1],exact:mode==='inventory'?(index+1)+'.000000000000000001':'0009007199254740993'+index,time:'2026-10-21T0'+index+':01:02Z'}));
      const size=limit==='5'?5:3,end=Math.min(start+size,all.length);const next=end===all.length?null:'segment:'+end+'+/==';if(next)cursors.set(next,end);
      return{entries:all.slice(start,end),continueAt:next};
    },
    onChange(state) {
      listStatus.set(state.error?'只读查询失败：'+state.error:state.loading?'正在读取当前作用域…':'已读取 '+state.rows.length+' 条合成记录。',{kind:state.error?'warning':state.loading?'info':'success'});
      $('more').disabled=state.loading||state.cursor===null;$('rows').replaceChildren(...state.rows.map(row=>{
        const tr=document.createElement('tr');for(const value of [row.title,row.kind,ui.formatExactText(row.exact),ui.formatTime(row.time,{timeZone:config.timeZone})]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
        const cell=document.createElement('td'),button=document.createElement('button');button.textContent='详情';button.className='secondary';button.addEventListener('click',()=>openDetails(row,button));cell.append(button);tr.append(cell);return tr;
      }));
    },
  });
  const refresh=append=>list.query({filters:{search:$('search').value},limit:$('limit').value,append});
  $('search').addEventListener('input',()=>refresh(false));$('limit').addEventListener('change',()=>refresh(false));$('refresh').addEventListener('click',()=>refresh(false));$('more').addEventListener('click',()=>refresh(true));refresh(false);

  const syntheticKey='synthetic.workbench.jobs.'+config.pack;
  let jobs={};try{jobs=JSON.parse(safeStorage?.getItem(syntheticKey)||'{}');}catch(_){}
  const syntheticFetch=async(path,init={})=>{
    if(init.method&&init.method!=='GET')throw new Error('合成任务只允许查询');
    if($('task-failure').checked)throw new Error('模拟进度查询断开');
    const id=new URL(path,location.origin).searchParams.get('id'),job=jobs[id];
    const data=!job?{status:'done',resultExpired:true}:{status:Date.now()<job.readyAt?'running':'done',result:{ok:null,status:'incomplete',synthetic:true,businessStatus:'仍待实际业务验收',reference:id}};
    return{json:async()=>({ok:true,data})};
  };
  const tasks=ui.createTaskController({pack:config.pack,page:config.page,scope:'synthetic-browser-only',storage:safeStorage,fetcher:syntheticFetch,execution:window.PLATFORMExecution,pollOptions:{intervalMs:220},
    onNotice:message=>taskStatus.set(message,{kind:'warning'}),
    onState(state,task){$('start-task').disabled=state!=='terminal';$('resume-task').disabled=!['pending','unavailable'].includes(state);taskStatus.set(state==='terminal'?'合成任务已结束；业务状态仍需独立判断。':state==='unavailable'?'查询中断，原标识 '+task.executionId+' 已保留，请恢复原任务查询。':(state==='pending'?'发现待确认的原任务 ':'正在查询原任务 ')+task.executionId,{kind:state==='unavailable'?'warning':'info'});},
    onResult(result){$('task-result').textContent=JSON.stringify(result,null,2);},
  });
  const watch=()=>tasks.watch().catch(()=>{});
  $('start-task').addEventListener('click',()=>{const executionId='synthetic-'+(window.crypto?.randomUUID?.()||Date.now());jobs[executionId]={readyAt:Date.now()+2200};try{safeStorage?.setItem(syntheticKey,JSON.stringify(jobs));}catch(_){}tasks.remember({executionId});watch();});
  $('resume-task').addEventListener('click',watch);
  if(!tasks.restore()){$('resume-task').disabled=true;taskStatus.set('尚未启动合成任务。');}
})();
