"""Hard deadline/cancellation for Pack-declared readonly cases; no write rollback claims."""
import json,math,os,signal,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def _terminate(process):
    try:
        if os.name=='posix':os.killpg(process.pid,signal.SIGTERM)
        else:process.terminate()
    except ProcessLookupError:pass
    try:process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            if os.name=='posix':os.killpg(process.pid,signal.SIGKILL)
            else:process.kill()
        except ProcessLookupError:pass
        process.wait(timeout=2)
    # A child may exit before a descendant; finish killing the owned process group.
    if os.name=='posix':
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass

def run_isolated(case_id,options,timeout_seconds,*,pack_id,root=ROOT):
    from execution_jobs import checkpoint,JobCancelled,JobTimedOut
    if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=3600:raise ValueError('invalid isolated case timeout')
    root=Path(root).resolve()
    def incomplete(status):
        return {'id':case_id,'ok':None,'status':'incomplete','executionStatus':status,'isolation':{'terminated':True},
                'subs':[{'label':'isolated runner','ok':None,'status':'incomplete','detail':'Owned readonly worker stopped: '+status+'; no business rollback is inferred.'}]}
    checkpoint()
    with tempfile.TemporaryDirectory(prefix='platform-case-') as temp:
        directory=Path(temp);input_file=directory/'input.json';output_file=directory/'output.json'
        input_file.write_text(json.dumps(options or {},allow_nan=False));input_file.chmod(0o600)
        env=dict(os.environ);env.update({'PLATFORM_PACKS':pack_id,'PLATFORM_ENV_FILE':'/dev/null','PLATFORM_ISOLATED_WORKER':'1','PYTHONDONTWRITEBYTECODE':'1'})
        if root!=ROOT:
            env['PLATFORM_PACK_DATA_DIR']=str(root/'data'/pack_id);env['PLATFORM_ARTIFACT_ROOT']=str(root/'artifacts'/pack_id)
        flags=['-S'] if os.environ.get('PLATFORM_OFFLINE_CONTEXT') and root!=ROOT else []
        args=[sys.executable,*flags,str(root/'scripts/isolated_case_worker.py'),'--case',case_id,'--input',str(input_file),'--output',str(output_file)]
        with (directory/'stderr.log').open('wb') as error:
            process=subprocess.Popen(args,cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=error,start_new_session=True)
            deadline=time.monotonic()+timeout_seconds
            try:
                while process.poll() is None:
                    try:checkpoint()
                    except (JobCancelled,JobTimedOut) as exc:
                        _terminate(process);return incomplete('cancelled' if isinstance(exc,JobCancelled) else 'timed_out')
                    if time.monotonic()>=deadline:_terminate(process);return incomplete('timed_out')
                    time.sleep(.025)
                if process.returncode or not output_file.is_file():
                    return {'id':case_id,'ok':False,'status':'failed','subs':[{'label':'isolated runner','ok':False,'detail':'Worker exited without a valid case result; inspect owned Pack code.'}]}
                result=json.loads(output_file.read_text())
                if not isinstance(result,dict):raise ValueError('isolated worker result must be an object')
                result['isolation']={'terminated':False};return result
            finally:
                _terminate(process)
