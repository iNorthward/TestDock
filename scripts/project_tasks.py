#!/usr/bin/env python3
"""Owned, evidence-backed Agent handoff checklist; never stores report contents."""
from __future__ import annotations
import argparse,hashlib,json,re,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from platform_capabilities import CAPABILITIES,adoption_report,build_binding,prepare_evidence
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
    owner=root/'packs'/pack_id
    if not manifest.is_file() or owner.resolve()!=owner or not manifest.resolve().is_relative_to(owner):raise ValueError('owned Pack manifest required')
    destination=root/'data'/pack_id/'onboarding-tasks.json'
    if not destination.resolve().is_relative_to(root/'data'/pack_id):raise ValueError('task state escapes project data')
    if (root/'data'/pack_id).resolve()!=root/'data'/pack_id:raise ValueError('project task directory escapes platform')
    return destination

def load(pack_id,*,root=ROOT,require_real=False):
    path=state_path(pack_id,root)
    if path.exists():
        data=json.loads(path.read_text())
        if not isinstance(data,dict) or data.get('pack')!=pack_id or data.get('schemaVersion')!=1:raise ValueError('foreign task state')
        rows=data.get('tasks')
        if (type(data.get('requireReal')) is not bool or not isinstance(rows,list)
                or len(rows)!=len(TASKS)
                or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) for row in rows)
                or {row['id'] for row in rows}!=set(TASKS)):
            raise ValueError('task state must contain every declared task exactly once')
        for row in rows:
            required=row['id']!='real_readonly' or data['requireReal']
            if row.get('required') is not required or row.get('status') not in ('pending','passed','stale'):
                raise ValueError('task state has invalid requirements or status')
        return data
    return {'schemaVersion':1,'pack':pack_id,'requireReal':require_real,'tasks':[
        {'id':key,'required':key!='real_readonly' or require_real,'status':'pending','nextAction':value}
        for key,value in TASKS.items()]}

def save(data,root):
    path=state_path(data['pack'],root);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    if temporary.exists():raise ValueError('pending task write exists; inspect before retry')
    saved={key:value for key,value in data.items() if key!='capabilityAdoption'}
    with temporary.open('x') as stream:stream.write(json.dumps(saved,ensure_ascii=False,indent=2)+'\n')
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
        return report.get('authorizedReadOnly') is True and _valid_case_ids(report.get('caseIds'))
    if task in {'environment','adapter','mock'}:
        raw_checks=report.get('checks')
        if not isinstance(raw_checks,list):return False
        checks={c.get('name'):c.get('status') for c in raw_checks if isinstance(c,dict) and isinstance(c.get('name'),str)}
        if task=='environment':return report.get('status')=='ready' and checks.get('environment')=='passed'
        return checks.get('catalog-adapter')=='passed' and (task!='mock' or checks.get('mock-smoke')=='passed')
    if task=='regression':
        return (report.get('ok') is True and type(report.get('testsRun')) is int and report['testsRun']>0
                and isinstance(report.get('groups'),list) and bool(report['groups'])
                and all(isinstance(g,str) and g.startswith(pack_id+'.') for g in report['groups'])
                and isinstance(report.get('offlineIsolation'),dict)
                and report['offlineIsolation'].get('temporaryDataRemoved') is True)
    if task=='real_readonly':
        return (report.get('ok') is True and report.get('scope')=='real_readonly'
                and report.get('realBusinessVerified') is True and _valid_case_ids(report.get('caseIds')))
    raise ValueError('unknown task')

def _valid_case_ids(values):
    return (isinstance(values,list) and bool(values)
            and all(isinstance(v,str) and bool(v) and v==v.strip() for v in values)
            and len(values)==len(set(values)))

def _read_evidence(path):
    raw=Path(path).read_bytes()
    return json.loads(raw),hashlib.sha256(raw).hexdigest()

def record(pack_id,task,evidence,*,root=ROOT):
    if task not in TASKS:raise ValueError('unknown task')
    data=status(pack_id,root=root);path=Path(evidence).resolve()
    report,digest=_read_evidence(path)
    if not validate_evidence(pack_id,task,report):raise ValueError('evidence does not prove this task passed')
    if task=='real_readonly':
        scope=next(t for t in data['tasks'] if t['id']=='scope')
        if scope['status']!='passed':raise ValueError('authorized scope evidence required first')
        declared,scope_digest=_read_evidence(scope['evidencePath'])
        if scope_digest!=scope['evidenceSha256'] or not validate_evidence(pack_id,'scope',declared):
            raise ValueError('scope evidence changed')
        if not set(report['caseIds'])<=set(declared['caseIds']):raise ValueError('real case IDs exceed declared scope')
    for row in data['tasks']:
        if row['id']==task:
            row.update(status='passed',evidencePath=str(path),evidenceSha256=digest,recordedAt=int(time.time()))
            if task=='real_readonly':row['scopeEvidenceSha256']=scope_digest
    save(data,root);return status(pack_id,root=root)

def _manifest(pack_id,root):
    # state_path already enforces selected-Pack ownership; do not import bootstrap.
    state_path(pack_id,root)
    path=Path(root).resolve()/'packs'/pack_id/'pack.json'
    return json.loads(path.read_bytes())

def capability_context(pack_id,capability,*,root=ROOT):
    manifest=_manifest(pack_id,root)
    return {'pack':pack_id,'capability':capability,
            'binding':build_binding(manifest,root=root,pack_id=pack_id,capability=capability)}

def record_capability(pack_id,capability,evidence,*,root=ROOT):
    manifest=_manifest(pack_id,root)
    receipt=prepare_evidence(manifest,capability,evidence,root=root,pack_id=pack_id,recorded_at=int(time.time()))
    data=load(pack_id,root=root)
    receipts=data.setdefault('capabilityEvidence',{})
    if not isinstance(receipts,dict):raise ValueError('invalid capability evidence receipts')
    receipts[capability]=receipt
    # Validate every receipt before persisting; preserve the original six task rows.
    adoption_report(manifest,root=root,pack_id=pack_id,capability_evidence=receipts)
    save(data,root);return status(pack_id,root=root)

def status(pack_id,*,root=ROOT):
    data=load(pack_id,root=root)
    reports={}
    for row in data['tasks']:
        if row['status']=='passed':
            try:
                report,digest=_read_evidence(row['evidencePath'])
                valid=digest==row['evidenceSha256'] and validate_evidence(pack_id,row['id'],report)
            except (OSError,ValueError,KeyError):valid=False
            if valid:reports[row['id']]=report
            else:
                row['status']='stale';row['nextAction']='Regenerate and re-record changed or missing evidence'
    scope=next(row for row in data['tasks'] if row['id']=='scope')
    real=next(row for row in data['tasks'] if row['id']=='real_readonly')
    if real['status']=='passed' and (scope['status']!='passed'
            or real.get('scopeEvidenceSha256',scope.get('evidenceSha256'))!=scope.get('evidenceSha256')
            or not set(reports['real_readonly']['caseIds'])<=set(reports['scope']['caseIds'])):
        real.update(status='stale',nextAction='Re-record real acceptance against the current authorized scope')
    capabilities=adoption_report(_manifest(pack_id,root),root=root,pack_id=pack_id,
                                 capability_evidence=data.get('capabilityEvidence',{}))
    ready=all(row['status']=='passed' for row in data['tasks'] if row['required']) and capabilities['status']=='ready'
    overall='failed' if capabilities['status']=='failed' else 'ready' if ready else 'incomplete'
    data.update(ok=True if ready else False if overall=='failed' else None,status=overall,
                realBusinessVerified=any(row['id']=='real_readonly' and row['status']=='passed' for row in data['tasks']),
                capabilityAdoption=capabilities)
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pack',required=True);p.add_argument('action',choices=['init','status','record','capability-context','record-capability']);p.add_argument('--require-real',action='store_true');p.add_argument('--task',choices=list(TASKS));p.add_argument('--capability',choices=CAPABILITIES);p.add_argument('--evidence',type=Path);a=p.parse_args()
    try:
        if a.action=='init':d=init(a.pack,require_real=a.require_real)
        elif a.action=='status':d=status(a.pack)
        elif a.action=='capability-context':
            if not a.capability:raise ValueError('capability-context requires --capability')
            d=capability_context(a.pack,a.capability)
        elif a.action=='record-capability':
            if not a.capability or not a.evidence:raise ValueError('record-capability requires --capability and --evidence')
            d=record_capability(a.pack,a.capability,a.evidence)
        else:
            if not a.task or not a.evidence:raise ValueError('record requires --task and --evidence')
            d=record(a.pack,a.task,a.evidence)
        print(json.dumps(d,ensure_ascii=False,indent=2));return 0 if a.action!='status' or d['ok'] else 2
    except (ValueError,OSError,KeyError) as exc:p.exit(2,str(exc)+'\n')
if __name__=='__main__':raise SystemExit(main())
