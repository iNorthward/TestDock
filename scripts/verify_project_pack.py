#!/usr/bin/env python3
"""Validate a selected Pack in a fresh environment; mock smoke forbids sockets."""
from __future__ import annotations
import argparse,json,os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
WORKER=r"""
import json,socket,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root),str(root/'test-platform'),str(root/'scripts')]
def denied(*a,**k): raise RuntimeError('Mock acceptance forbids network; provide an offline transport')
for name in ('connect','connect_ex','sendto','sendmsg'):
    if hasattr(socket.socket,name):setattr(socket.socket,name,denied)
for name in ('create_connection','getaddrinfo','gethostbyname','gethostbyname_ex','gethostbyaddr'):setattr(socket,name,denied)
from pack_registry import load_packs
pack=load_packs()[0]
import catalog
snapshot=catalog.catalog()
from validate_catalog import check_structure
errors,warnings,stats=check_structure()
if errors:raise RuntimeError('Catalog validation failed: '+str(errors))
result={'pack':pack['id'],'cases':len(snapshot.get('cases',[])),'scope':'offline validation only','warnings':warnings}
if len(sys.argv)>2:
    identifier=sys.argv[2]
    cases=snapshot.get('cases',[])
    row=next((c for c in cases if c['id']==identifier),None)
    if not row or row.get('mode')!='safe':raise RuntimeError('Smoke case must exist and be SAFE')
    output=catalog.exec_case(identifier)
    if output.get('status')!='passed' or output.get('ok') is not True:raise RuntimeError('Smoke case did not pass: '+str(output))
    result['mockPassed']=identifier
print(json.dumps(result,ensure_ascii=False))
"""
def verify(pack_id,*,smoke=None,root=ROOT):
    if not pack_id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in pack_id):raise ValueError('Invalid pack id')
    with tempfile.TemporaryDirectory(prefix='pack-acceptance-') as tmp:
        env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR','LANG') if k in os.environ}
        env.update({'PLATFORM_ENV_FILE':'/dev/null','PLATFORM_PACKS':pack_id,'PLATFORM_PACK_DATA_DIR':tmp+'/data','PLATFORM_ARTIFACT_ROOT':tmp+'/artifacts','PYTHONDONTWRITEBYTECODE':'1','TZ':'Asia/Shanghai'})
        # A temporary distribution must not inherit sitecustomize imports from
        # the parent checkout. Native regression isolation still covers it.
        flags=['-S'] if os.environ.get('PLATFORM_OFFLINE_CONTEXT') and Path(root).resolve()!=ROOT else []
        args=[sys.executable,*flags,'-c',WORKER,str(Path(root).resolve())]+([smoke] if smoke else [])
        proc=subprocess.run(args,cwd=root,env=env,text=True,capture_output=True,timeout=60)
        if proc.returncode:raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return json.loads(proc.stdout.strip().splitlines()[-1])
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pack',required=True);p.add_argument('--mock-smoke',action='store_true');p.add_argument('--case-id',default='DEMO-R01');a=p.parse_args()
    try:print(json.dumps(verify(a.pack,smoke=a.case_id if a.mock_smoke else None),ensure_ascii=False,indent=2))
    except (ValueError,RuntimeError,subprocess.TimeoutExpired) as e:p.exit(2,str(e)+'\n')
