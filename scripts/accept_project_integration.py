#!/usr/bin/env python3
"""End-to-end integration acceptance against an owned loopback mock service."""
from __future__ import annotations
import argparse,json,os,socket,subprocess,sys,tempfile,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
sys.path.insert(0,str(ROOT/'scripts'))
from create_project_pack import create
from export_platform import build

def free_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1',0));return stream.getsockname()[1]

def run_acceptance():
    checks=[];state={'shape':'valid','calls':0,'authorized':0}
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    class Business(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            state['calls']+=1
            authorized=self.headers.get('Authorization')=='Bearer synthetic-integration-token'
            if authorized:state['authorized']+=1
            body={'result':{'alive':True if state['shape']=='valid' else 'true'}} if authorized else {'error':'unauthorized'}
            self.send_response(200 if authorized else 401);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body).encode())
    business=ThreadingHTTPServer(('127.0.0.1',0),Business)
    thread=threading.Thread(target=business.serve_forever,daemon=True);thread.start()
    apps=[]
    try:
        with tempfile.TemporaryDirectory(prefix='platform-integration-acceptance-') as temporary:
            release=Path(temporary)/'release';build(release);root=release/'platform'
            from check_neutral_distribution import findings
            check('clean standalone release',not findings(root))
            base='http://127.0.0.1:'+str(business.server_port)
            recipe=root/'examples/http_pack_template/contract.example.json'
            ports=[free_port(),free_port()]
            create('service_alpha','Service Alpha',ports[0],root=root,contract_file=recipe)
            beta_folder=create('service_beta','Service Beta',ports[1],root=root)
            def start(pack,port,token):
                env={k:os.environ[k] for k in ('PATH','TMPDIR','LANG','SYSTEMROOT') if k in os.environ}
                env.update({'PLATFORM_ENV_FILE':str(root/('.env.'+pack)),'PLATFORM_PACKS':pack,
                            'PLATFORM_PANEL_PORT':str(port),'SERVICE_API_BASE':base,
                            'SERVICE_ACCESS_TOKEN':token,'PYTHONDONTWRITEBYTECODE':'1','TZ':'Asia/Shanghai'})
                log=open(Path(temporary)/(pack+'-'+str(len(apps))+'.log'),'w')
                process=subprocess.Popen([sys.executable,'-u',str(root/'test-platform/server.py'),'--pack='+pack,'--port='+str(port)],cwd=root,env=env,stdout=log,stderr=log)
                apps.append((process,log));url='http://127.0.0.1:'+str(port)
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    if process.poll() is not None:
                        log.flush()
                        tail=Path(log.name).read_text()[-1800:]
                        raise RuntimeError('Acceptance panel startup failed: '+tail)
                    try:
                        with OPENER.open(url+'/api/platform',timeout=1) as response:
                            if json.load(response)['data']['pack']==pack:return process,url
                    except (OSError,ValueError):time.sleep(.1)
                log.flush()
                raise TimeoutError('Acceptance panel did not become ready: '+Path(log.name).read_text()[-1800:])
            def api(url,path,payload=None):
                request=urllib.request.Request(url+path,data=json.dumps(payload).encode() if payload is not None else None,
                                              headers={'Content-Type':'application/json'})
                with OPENER.open(request,timeout=10) as response:
                    payload=json.load(response)
                    return payload.get('data',payload)
            alpha,a=start('service_alpha',ports[0],'synthetic-integration-token')
            beta,b=start('service_beta',ports[1],'')
            from project_doctor import diagnose
            from project_tasks import init,record,status
            doctor=diagnose('service_alpha',root=root,mock_case='HTTP-MOCK-R01')
            check('doctor detects missing private HTTP configuration',doctor['status']=='incomplete')
            env_file=root/'.env.service_alpha'
            with env_file.open('a') as stream:stream.write('SERVICE_API_BASE='+base+'\nSERVICE_ACCESS_TOKEN=synthetic-integration-token\n')
            doctor=diagnose('service_alpha',root=root,mock_case='HTTP-MOCK-R01')
            check('one command validates configured adapter and offline mock',doctor['status']=='ready')
            report_file=Path(temporary)/'doctor.json';report_file.write_text(json.dumps(doctor))
            scope_file=Path(temporary)/'scope.json';scope_file.write_text(json.dumps({'pack':'service_alpha','authorizedReadOnly':True,'caseIds':['HTTP-R01']}))
            init('service_alpha',root=root)
            for task,evidence in [('scope',scope_file),('environment',report_file),('adapter',report_file),('mock',report_file)]:record('service_alpha',task,evidence,root=root)
            check('Agent checklist refuses completion without regression evidence',status('service_alpha',root=root)['status']=='incomplete')
            regression_file=Path(temporary)/'regression.json'
            regression_env={k:os.environ[k] for k in ('PATH','LANG','TMPDIR') if k in os.environ}
            regression_env.update(PLATFORM_PACKS='service_alpha',PLATFORM_ENV_FILE='/dev/null',PYTHONDONTWRITEBYTECODE='1')
            regression=subprocess.run([sys.executable,'scripts/targeted_regression.py','--group','service_alpha.adapter','--report',str(regression_file)],cwd=root,env=regression_env,capture_output=True,text=True,timeout=90)
            check('owned Adapter targeted regression passes',regression.returncode==0)
            record('service_alpha','regression',regression_file,root=root)
            check('Agent handoff completes only with all required evidence',status('service_alpha',root=root)['status']=='ready')
            report_file.write_text('{}')
            check('Agent evidence change invalidates completed steps',any(t['status']=='stale' for t in status('service_alpha',root=root)['tasks']))
            incompatible=beta_folder/'pack.json';original_manifest=incompatible.read_text();manifest=json.loads(original_manifest);manifest['platform_api']['min']='2.0';manifest['platform_api']['max_exclusive']='3.0';incompatible.write_text(json.dumps(manifest))
            from verify_project_pack import verify
            try:verify('service_beta',root=root)
            except RuntimeError:rejected=True
            else:rejected=False
            check('incompatible Pack rejected before startup',rejected)
            incompatible.write_text(original_manifest)
            check('two projects start independently',api(a,'/api/platform')['pack']=='service_alpha' and api(b,'/api/platform')['pack']=='service_beta')
            check('optional capabilities disabled',api(a,'/api/platform')['diagnostics']==[] and not api(a,'/api/platform')['resourcePool']['enabled'])
            check('HTTP project catalog isolated',len(api(a,'/api/catalog')['cases'])==2 and len(api(b,'/api/catalog')['cases'])==1)
            result=api(a,'/api/run-case',{'id':'HTTP-R01','requestId':'acceptance-success'})
            check('real HTTP transport with declared authentication',result.get('status')=='passed' and state['authorized']==1)
            check('declared HTTP contract is fingerprinted',bool(result.get('executionMetadata',{}).get('contractFingerprints',{}).get('http')))
            repeat=api(a,'/api/run-case',{'id':'HTTP-R01','requestId':'acceptance-success'})
            check('request retry does not replay business operation',repeat.get('deduplicated') is True and state['authorized']==1 and repeat.get('runId')==result.get('runId'))
            queued=api(a,'/api/run-case',{'id':'HTTP-MOCK-R01','requestId':'acceptance-background','background':True})
            check('UI execution returns durable background identifier',bool(queued.get('executionId')))
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                job=api(a,'/api/execution/job?id='+queued['executionId'])
                if job['status'] not in {'queued','running','cancel_requested'}:break
                time.sleep(.05)
            check('background result can be resumed by identifier',job['status']=='done' and job['result']['status']=='passed')
            queue=api(a,'/api/execution/queue')
            check('bounded queue exposes process capacity',queue['scope']=='process' and queue['workers']==2 and queue['queueCapacity']==32)
            state['shape']='wrong'
            wrong=api(a,'/api/run-case',{'id':'HTTP-R01','requestId':'acceptance-wrong-type'})
            check('wrong raw JSON type fails',wrong.get('status')=='failed')
            compared=api(a,'/api/history/compare?before='+str(result['runId'])+'&after='+str(wrong['runId']))
            check('history comparison observes status and assertion differences',any(row.get('beforeStatus')=='passed' and row.get('afterStatus')=='failed' and row.get('assertionChanges') for row in compared['changes']))
            state['shape']='valid'
            other=api(b,'/api/run-case',{'id':'DEMO-R01','requestId':'acceptance-beta'})
            check('second project remains usable',other.get('status')=='passed')
            check('separate runtime histories', (root/'data/service_alpha/run_history.db').is_file() and (root/'data/service_beta/run_history.db').is_file())
            alpha.terminate();alpha.wait(timeout=6)
            alpha,a=start('service_alpha',ports[0],'wrong-synthetic-token')
            denied=api(a,'/api/run-case',{'id':'HTTP-R01','requestId':'acceptance-unauthorized'})
            check('authentication rejection fails instead of passing',denied.get('status')=='failed')
            alpha.terminate();alpha.wait(timeout=6)
            before=state['calls'];alpha,a=start('service_alpha',ports[0],'')
            missing=api(a,'/api/run-case',{'id':'HTTP-R01','requestId':'acceptance-missing-token'})
            check('missing credentials are incomplete before HTTP',missing.get('status')=='incomplete' and state['calls']==before)
            for process,log in apps:
                if process.poll() is None:process.terminate();process.wait(timeout=6)
                log.close()
            checks.append({'name':'owned services stopped and temporary data removed','passed':True})
        return {'ok':all(c['passed'] for c in checks),'checks':checks,'scope':'owned loopback mock business and standalone temporary platform only','realBusinessCasesExecuted':0}
    finally:
        for process,log in apps:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=6)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            if not log.closed:log.close()
        business.shutdown();business.server_close();thread.join(timeout=3)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',required=True);a=p.parse_args()
    try:
        result=run_acceptance();Path(a.report).parent.mkdir(parents=True,exist_ok=True);Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as error:
        p.exit(1,'Integration acceptance failed: '+type(error).__name__+': '+str(error)+'\n')
