#!/usr/bin/env python3
"""Owned, evidence-backed Agent handoff checklist; never stores report contents."""
from __future__ import annotations
import argparse,hashlib,json,re,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TASKS={
 'scope':'Record authorized read-only case IDs and project evidence',
 'environment':'Run project_doctor.py with the selected environment',
 'adapter':'Run project_doctor.py and fix catalog/bootstrap findings',
 'mock':'Run project_doctor.py --mock-case <owned offline case>',
 'regression':'Run targeted_regression.py --group <pack>.adapter',
 'real_readonly':'Execute only authorized real read-only cases and preserve a sanitized report',
}

def state_path(pack_id,root=ROOT):
    root=Path(root).resolve()
    if not re.fullmatch('[a-z][a-z0-9_]*',pack_id):raise ValueError('invalid pack id')
    manifest=root/'packs'/pack_id/'pack.json'
    if not manifest.is_file() or not manifest.resolve().is_relative_to(root/'packs'):raise ValueError('owned Pack manifest required')
    destination=root/'data'/pack_id/'onboarding-tasks.json'
    if not destination.resolve().is_relative_to(root/'data'/pack_id):raise ValueError('task state escapes project data')
    if not (root/'data'/pack_id).resolve().is_relative_to(root):raise ValueError('project task directory escapes platform')
    return destination

def load(pack_id,*,root=ROOT,require_real=False):
    path=state_path(pack_id,root)
    if path.exists():
        data=json.loads(path.read_text())
        if data.get('pack')!=pack_id or data.get('schemaVersion')!=1:raise ValueError('foreign task state')
        return data
    return {'schemaVersion':1,'pack':pack_id,'requireReal':require_real,'tasks':[
        {'id':key,'required':key!='real_readonly' or require_real,'status':'pending','nextAction':value}
        for key,value in TASKS.items()]}

def save(data,root):
    path=state_path(data['pack'],root);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    if temporary.exists():raise ValueError('pending task write exists; inspect before retry')
    with temporary.open('x') as stream:stream.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temporary.chmod(0o600);temporary.replace(path)

def init(pack_id,*,root=ROOT,require_real=False):
    data=load(pack_id,root=root,require_real=require_real)
    if require_real:
        data['requireReal']=True
        for task in data['tasks']:
            if task['id']=='real_readonly':task['required']=True
    save(data,root);return status(pack_id,root=root)

def validate_evidence(pack_id,task,report):
    if not isinstance(report,dict):raise ValueError('evidence must be a JSON object')
    if task!='regression' and report.get('pack')!=pack_id:raise ValueError('evidence belongs to another Pack')
    if task=='scope':
        return report.get('authorizedReadOnly') is True and isinstance(report.get('caseIds'),list) and bool(report['caseIds']) and all(isinstance(v,str) and v for v in report['caseIds'])
    if task in {'environment','adapter','mock'}:
        checks={c.get('name'):c.get('status') for c in report.get('checks',[]) if isinstance(c,dict)}
        if task=='environment':return report.get('status')=='ready' and checks.get('environment')=='passed'
        return checks.get('catalog-adapter')=='passed' and (task!='mock' or checks.get('mock-smoke')=='passed')
    if task=='regression':
        return (report.get('ok') is True and type(report.get('testsRun')) is int and report['testsRun']>0
                and bool(report.get('groups')) and all(isinstance(g,str) and g.startswith(pack_id+'.') for g in report['groups'])
                and report.get('offlineIsolation',{}).get('temporaryDataRemoved') is True)
    if task=='real_readonly':
        return (report.get('ok') is True and report.get('scope')=='real_readonly'
                and report.get('realBusinessVerified') is True and isinstance(report.get('caseIds'),list) and bool(report['caseIds']))
    raise ValueError('unknown task')

def record(pack_id,task,evidence,*,root=ROOT):
    if task not in TASKS:raise ValueError('unknown task')
    data=load(pack_id,root=root);path=Path(evidence).resolve()
    report=json.loads(path.read_text())
    if not validate_evidence(pack_id,task,report):raise ValueError('evidence does not prove this task passed')
    if task=='real_readonly':
        scope=next(t for t in data['tasks'] if t['id']=='scope')
        if scope['status']!='passed':raise ValueError('authorized scope evidence required first')
        declared=json.loads(Path(scope['evidencePath']).read_text())
        if hashlib.sha256(Path(scope['evidencePath']).read_bytes()).hexdigest()!=scope['evidenceSha256']:
            raise ValueError('scope evidence changed')
        if not set(report['caseIds'])<=set(declared['caseIds']):raise ValueError('real case IDs exceed declared scope')
    for row in data['tasks']:
        if row['id']==task:row.update(status='passed',evidencePath=str(path),evidenceSha256=hashlib.sha256(path.read_bytes()).hexdigest(),recordedAt=int(time.time()))
    save(data,root);return status(pack_id,root=root)

def status(pack_id,*,root=ROOT):
    data=load(pack_id,root=root)
    for row in data['tasks']:
        if row['status']=='passed':
            path=Path(row['evidencePath'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['evidenceSha256']:
                row['status']='stale';row['nextAction']='Regenerate and re-record changed or missing evidence'
    ready=all(row['status']=='passed' for row in data['tasks'] if row['required'])
    data.update(ok=ready,status='ready' if ready else 'incomplete',realBusinessVerified=any(row['id']=='real_readonly' and row['status']=='passed' for row in data['tasks']))
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pack',required=True);p.add_argument('action',choices=['init','status','record']);p.add_argument('--require-real',action='store_true');p.add_argument('--task',choices=list(TASKS));p.add_argument('--evidence',type=Path);a=p.parse_args()
    try:
        if a.action=='init':d=init(a.pack,require_real=a.require_real)
        elif a.action=='status':d=status(a.pack)
        else:
            if not a.task or not a.evidence:raise ValueError('record requires --task and --evidence')
            d=record(a.pack,a.task,a.evidence)
        print(json.dumps(d,ensure_ascii=False,indent=2));return 0 if a.action!='status' or d['ok'] else 2
    except (ValueError,OSError,KeyError) as exc:p.exit(2,str(exc)+'\n')
if __name__=='__main__':raise SystemExit(main())
