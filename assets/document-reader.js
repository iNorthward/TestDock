const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escapeAttr=s=>esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const documentPath=location.pathname==='/whitepaper'?'docs/architecture/TEST_PLATFORM_ARCHITECTURE.md':decodeURIComponent(location.pathname.slice(1));
const documentBase=new URL('/'+documentPath,location.origin);
function mdInline(source){
  const tokens=[];
  const stash=html=>{const key='\u0000'+tokens.length+'\u0000';tokens.push(html);return key;};
  let text=String(source).replace(/`([^`]+)`/g,(_,code)=>stash('<code>'+esc(code)+'</code>'));
  text=text.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(_,label,target)=>{
    try{
      const url=new URL(target,documentBase);
      if(!['http:','https:'].includes(url.protocol))return label;
      return stash('<a href="'+escapeAttr(url.href)+'">'+esc(label)+'</a>');
    }catch(error){return label;}
  });
  text=esc(text).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  text=text.replace(/(^|\s)_([^_]+)_(?=\s|$|[。，、])/g,'$1<em>$2</em>');
  return text.replace(/\u0000(\d+)\u0000/g,(_,index)=>tokens[Number(index)]);
}

function mdTableRows(rows){
  const parse=r=>r.split('|').slice(1,-1).map(c=>c.trim());
  const head=parse(rows[0]);
  const body=rows.slice(2);
  return `<table class="md-table"><thead><tr>${head.map(h=>`<th>${mdInline(h)}</th>`).join('')}</tr></thead><tbody>${body.map(r=>`<tr>${parse(r).map(c=>`<td>${mdInline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function renderInspectMd(src){
  const lines=String(src||'').replace(/\r\n/g,'\n').split('\n');
  const out=[];
  let i=0;
  while(i<lines.length){
    const line=lines[i];
    if(/^```/.test(line)){
      const code=[]; i++;
      while(i<lines.length&&!/^```/.test(lines[i])){code.push(lines[i]);i++;}
      out.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');
      i++; continue;
    }
    if(/^\|.+\|$/.test(line.trim())){
      const rows=[];
      while(i<lines.length&&/^\|.+\|$/.test(lines[i].trim())){rows.push(lines[i].trim());i++;}
      if(rows.length>=2) out.push(mdTableRows(rows));
      continue;
    }
    const hm=line.match(/^(#{1,6}) (.+)$/);
    if(hm){
      const tag='h'+hm[1].length;
      out.push(`<${tag}>${mdInline(hm[2])}</${tag}>`);
      i++; continue;
    }
    if(/^> /.test(line)){
      const bq=[];
      while(i<lines.length&&/^> /.test(lines[i])){bq.push(lines[i].slice(2));i++;}
      out.push('<blockquote>'+bq.map(mdInline).join('<br>')+'</blockquote>');
      continue;
    }
    if(/^[-*] /.test(line)){
      const items=[];
      while(i<lines.length&&/^[-*] /.test(lines[i])){items.push('<li>'+mdInline(lines[i].slice(2))+'</li>');i++;}
      out.push('<ul>'+items.join('')+'</ul>');
      continue;
    }
    if(/^_{3,}$|^\*{3,}$|^-{3,}$/.test(line.trim())){out.push('<hr>');i++;continue;}
    if(!line.trim()){i++;continue;}
    out.push('<p>'+mdInline(line)+'</p>');
    i++;
  }
  return out.join('\n');
}


(async()=>{
 const content=document.getElementById('document-content');
 try{
  const response=await fetch('/api/document?path='+encodeURIComponent(documentPath));
  const payload=await response.json();if(!response.ok||!payload.ok)throw Error(payload.error||'文档请求失败');
  content.innerHTML=renderInspectMd(payload.data.content);
  const heading=content.querySelector('h1');if(heading)document.querySelector('.wp').prepend(heading);document.title=heading?.textContent||'平台文档';window.dispatchEvent(new CustomEvent('document-loaded',{detail:location.pathname==='/whitepaper'?'平台白皮书':document.title}));
  const toc=document.getElementById('document-toc');
  content.querySelectorAll('h2,h3').forEach((heading,index)=>{
   heading.id='section-'+index;const link=document.createElement('a');link.href='#'+heading.id;link.textContent=heading.textContent;toc.append(link);
  });
 }catch(error){content.textContent='文档加载失败：'+error.message;}
})();

