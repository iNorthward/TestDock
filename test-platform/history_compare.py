"""Compare stored results and execution evidence without rerunning any case."""
from identity_pool import redact_secrets

def _cases(run):
    values=run.get('cases',[])
    if not isinstance(values,list):raise ValueError('history cases must be an array')
    result={}
    for row in values:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in result:raise ValueError('history case ids must be present and unique')
        result[row['id']]=row
    return result

def _assertions(case):
    result={};occurrences={}
    for index,row in enumerate(case.get('subs',[])):
        if not isinstance(row,dict):continue
        label=str(row.get('label',index));occurrences[label]=occurrences.get(label,0)+1
        result[(label,occurrences[label])]=redact_secrets({k:row.get(k) for k in ('ok','status','detail')})
    return result

def compare_runs(before,after):
    if not isinstance(before,dict) or not isinstance(after,dict):raise ValueError('both history runs must exist')
    left=before.get('executionMetadata',{});right=after.get('executionMetadata',{})
    if left.get('packId') and right.get('packId') and left['packId']!=right['packId']:raise ValueError('cannot compare different Pack histories')
    old,new=_cases(before),_cases(after);changes=[];unchanged=0
    for identifier in sorted(old.keys()|new.keys()):
        a,b=old.get(identifier),new.get(identifier)
        if a is None or b is None:
            changes.append({'id':identifier,'kind':'added' if a is None else 'removed','beforeStatus':a.get('status') if a else None,'afterStatus':b.get('status') if b else None});continue
        assertions=[];sa,sb=_assertions(a),_assertions(b)
        for key in sorted(sa.keys()|sb.keys()):
            if sa.get(key)!=sb.get(key):assertions.append({'label':key[0],'occurrence':key[1],'before':sa.get(key),'after':sb.get(key)})
        if a.get('status')!=b.get('status') or assertions:
            changes.append({'id':identifier,'kind':'changed','beforeStatus':a.get('status'),'afterStatus':b.get('status'),'assertionChanges':assertions})
        else:unchanged+=1
    lc,rc=left.get('contractFingerprints',{}),right.get('contractFingerprints',{})
    contracts=[{'label':name,'before':lc.get(name),'after':rc.get(name)} for name in sorted(lc.keys()|rc.keys()) if lc.get(name)!=rc.get(name)]
    return {'beforeRunId':before.get('id'),'afterRunId':after.get('id'),'unchangedCases':unchanged,'changes':changes,
            'contractChanges':contracts,'contractEvidenceAvailable':bool(lc) and bool(rc),
            'codeChanged':left.get('codeFingerprint')!=right.get('codeFingerprint') if left.get('codeFingerprint') and right.get('codeFingerprint') else None,
            'packEvidenceAvailable':bool(left.get('packId')) and bool(right.get('packId')),
            'note':'Contract/code drift is correlation evidence, not proof of the failure cause. Missing historical fingerprints are not inferred.'}
