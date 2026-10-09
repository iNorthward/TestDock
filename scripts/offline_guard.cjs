'use strict';
// Loaded with NODE_OPTIONS before tests and inherited by Node subprocesses.
const fs = require('node:fs');
const path = require('node:path');
const context = JSON.parse(fs.readFileSync(process.env.PLATFORM_OFFLINE_CONTEXT, 'utf8'));
const append = fs.appendFileSync.bind(fs), realpath = fs.realpathSync.bind(fs);
function record(kind, source) {
  append(context.attempt_log, JSON.stringify({kind, source, pid:process.pid})+'\n', {mode:0o600});
}
function deny() {
  record('network', 'node.network');
  throw new Error('离线回归禁止网络连接（含 Node/子进程）');
}
for (const [name, methods] of [
  ['node:net', ['connect','createConnection']], ['node:tls', ['connect']],
  ['node:http', ['request','get']], ['node:https', ['request','get']],
  ['node:http2', ['connect']], ['node:dgram', ['createSocket']],
  ['node:dns', ['lookup','resolve','resolve4','resolve6','reverse','lookupService']],
]) {
  const module = require(name);
  for (const method of methods) if (typeof module[method]==='function') module[method]=deny;
}
require('node:net').Socket.prototype.connect=deny;
const dnsPromises=require('node:dns').promises;
for (const key of Object.keys(dnsPromises)) if(typeof dnsPromises[key]==='function'&&key!=='Resolver') dnsPromises[key]=deny;
globalThis.fetch=async()=>deny();
function resolved(value) {
  if(typeof value==='number')return null;
  if(value instanceof URL)value=require('node:url').fileURLToPath(value);
  if(Buffer.isBuffer(value))value=value.toString();
  if(typeof value!=='string')return null;
  const absolute=path.resolve(value);
  try{return realpath(absolute);}catch{return absolute;}
}
function protect(value, writing) {
  const target=resolved(value);
  if(!target)return;
  if(context.protected_reads.includes(target)||writing&&context.protected_roots.some(root=>target===root||target.startsWith(root+path.sep))){
    record('filesystem','node.fs');
    throw new Error('离线回归禁止访问真实凭据/账号池或写入源码目录');
  }
}
const writeMethods=['writeFile','appendFile','createWriteStream','unlink','rm','rmdir','mkdir','truncate','chmod','chown'];
for(const name of ['readFile', 'createReadStream', ...writeMethods]){
  for(const suffix of ['', 'Sync']){
    const key=name+suffix;
    if(typeof fs[key]!=='function')continue;
    const original=fs[key].bind(fs);
    fs[key]=(...args)=>{protect(args[0],writeMethods.includes(name));return original(...args);};
  }
  if(typeof fs.promises[name]==='function'){
    const original=fs.promises[name].bind(fs.promises);
    fs.promises[name]=async(...args)=>{protect(args[0],writeMethods.includes(name));return original(...args);};
  }
}
for(const name of ['rename','copyFile']){
  for(const suffix of ['', 'Sync']){
    const key=name+suffix, original=fs[key].bind(fs);
    fs[key]=(...args)=>{protect(args[0],name==='rename');protect(args[1],true);return original(...args);};
  }
  const original=fs.promises[name].bind(fs.promises);
  fs.promises[name]=async(...args)=>{protect(args[0],name==='rename');protect(args[1],true);return original(...args);};
}
function writeFlags(flags){
  return typeof flags==='number'?Boolean(flags&(fs.constants.O_WRONLY|fs.constants.O_RDWR|fs.constants.O_CREAT|fs.constants.O_TRUNC|fs.constants.O_APPEND)):/[wax+]/.test(flags||'r');
}
for(const suffix of ['', 'Sync']){
  const key='open'+suffix, original=fs[key].bind(fs);
  fs[key]=(...args)=>{protect(args[0],writeFlags(args[1]));return original(...args);};
}
const openPromise=fs.promises.open.bind(fs.promises);
fs.promises.open=async(...args)=>{protect(args[0],writeFlags(args[1]));return openPromise(...args);};
const cp=require('node:child_process');
function childOptions(options={}){
  const env={...(options.env||process.env)};
  env.PLATFORM_OFFLINE_CONTEXT=env.PLATFORM_OFFLINE_CONTEXT||process.env.PLATFORM_OFFLINE_CONTEXT;
  const selected=JSON.parse(fs.readFileSync(env.PLATFORM_OFFLINE_CONTEXT,'utf8'));
  env.NODE_OPTIONS='--require '+JSON.stringify(selected.node_guard);
  env.PYTHONPATH=[selected.startup_dir,selected.scripts,env.PYTHONPATH||''].join(path.delimiter);
  env.PYTHONDONTWRITEBYTECODE='1';
  env.PLATFORM_ENV_FILE='/dev/null';
  for(const [key, folder] of [['PLATFORM_PACK_DATA_DIR','data'],['PLATFORM_ARTIFACT_ROOT','artifacts']]){
    if(!env[key]||selected.protected_roots.some(root=>env[key]===root||env[key].startsWith(root+path.sep)))env[key]=path.join(selected.directory,folder);
  }
  return {...options,env};
}
for(const name of ['spawn','spawnSync','fork']){
  const original=cp[name].bind(cp);
  cp[name]=(command,args,options)=>Array.isArray(args)?original(command,args,childOptions(options)):original(command,childOptions(args));
}
for(const name of ['exec','execSync','execFile','execFileSync']){
  const original=cp[name].bind(cp);
  cp[name]=(...args)=>{
    const callback=typeof args.at(-1)==='function'?args.pop():null;
    let options=args.at(-1);
    if(options&&typeof options==='object'&&!Array.isArray(options))args[args.length-1]=childOptions(options);
    else args.push(childOptions());
    if(callback)args.push(callback);
    return original(...args);
  };
}
require('node:module').syncBuiltinESMExports();
