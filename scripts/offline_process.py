"""Fresh Python/Node offline workers with native network and source protection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import offline_runtime

CODE_ROOT=Path(__file__).resolve().parents[1]


def clean_environment(directory, pack_id, fixtures):
    safe_keys=('PATH','LANG','LC_ALL','LC_CTYPE','TZ','TERM','SYSTEMROOT','WINDIR','TMPDIR','TMP','TEMP')
    environment={key:os.environ[key] for key in safe_keys if key in os.environ}
    environment.update({'PLATFORM_PACKS':pack_id,'PLATFORM_ENV_FILE':'/dev/null','PYTHONDONTWRITEBYTECODE':'1',
                        'PLATFORM_PACK_DATA_DIR':str(directory/'data'),'PLATFORM_ARTIFACT_ROOT':str(directory/'artifacts'),
                        'PLATFORM_API_BASE':'https://offline.invalid','PLATFORM_AUTH_BASE':'https://offline.invalid',
                        'PLATFORM_NODE_API_BASE':'https://offline.invalid'})
    environment.update(fixtures)
    return environment


def system_command(command, profile):
    if offline_runtime.installed():
        return command, 'inherited-native-sandbox'
    if sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').is_file():
        return ['/usr/bin/sandbox-exec','-f',str(profile),*command], 'macOS-sandbox-exec'
    if sys.platform.startswith('linux') and shutil.which('bwrap'):
        # A network namespace alone leaves non-Python descendants able to write
        # real data. Bubblewrap supplies a read-only root and private temp space.
        context=json.loads((profile.parent/'context.json').read_text())
        arguments=['bwrap','--die-with-parent','--unshare-net','--ro-bind','/','/',
                   '--tmpfs','/tmp','--bind',str(profile.parent),str(profile.parent),
                   '--proc','/proc','--dev','/dev']
        for value in context['protected_reads']:
            if Path(value).is_file():
                arguments.extend(['--ro-bind','/dev/null',value])
        return [*arguments,'--',*command], 'Linux-bubblewrap'
    raise ValueError('[incomplete] 当前系统缺少可用的离线网络隔离后端，停止执行测试')


def execute(root, plan, *, fixtures=None, inputs=None):
    sys.path[:0]=[str(CODE_ROOT),str(CODE_ROOT/'test-platform')]
    from pack_registry import load_packs
    from timezone_config import configured_env_file
    pack=load_packs()[0]
    roots=sorted({str(Path(root).resolve()),str(CODE_ROOT.resolve())})
    pool=(pack['root']/str(pack.get('identity_pool_file') or 'data/identity_pool.json')).resolve()
    protected_reads=sorted({str(pool),str((CODE_ROOT/'.env.local').resolve()),str(configured_env_file().resolve())}-{'/dev/null'})
    python_result={'testsRun':0,'failures':0,'errors':0,'ok':True}
    node=None
    with tempfile.TemporaryDirectory(prefix='platform-offline-') as temporary:
        directory=Path(temporary).resolve()
        for name in ('data','artifacts','startup'):(directory/name).mkdir()
        context={'root':str(Path(root).resolve()),'code_root':str(CODE_ROOT),'scripts':str(CODE_ROOT/'scripts'),
                 'directory':str(directory),'startup_dir':str(directory/'startup'),'node_guard':str(CODE_ROOT/'scripts/offline_guard.cjs'),
                 'attempt_log':str(directory/'attempts.jsonl'),'protected_roots':roots,'protected_reads':protected_reads}
        config=directory/'context.json'
        config.write_text(json.dumps(context))
        config.chmod(0o600)
        (directory/'startup/sitecustomize.py').write_text('from offline_runtime import install\ninstall()\n')
        environment=clean_environment(directory,pack['id'],fixtures or {})
        environment.update(inputs or {})
        environment.update({'PLATFORM_OFFLINE_CONTEXT':str(config),'PYTHONPATH':os.pathsep.join([str(directory/'startup'),str(CODE_ROOT/'scripts')]),
                            'NODE_OPTIONS':'--require '+json.dumps(context['node_guard'])})
        profile=directory/'sandbox.sb'
        rules=['(version 1)','(allow default)','(deny network*)']
        rules+=['(deny file-write* (subpath '+json.dumps(value)+'))' for value in roots]
        rules+=['(deny file-read* (literal '+json.dumps(value)+'))' for value in protected_reads]
        profile.write_text('\n'.join(rules))
        payload=directory/'plan.json'
        payload.write_text(json.dumps(plan))
        backend=None
        if plan['python']:
            destination=directory/'python-result.json'
            command,backend=system_command([sys.executable,str(CODE_ROOT/'scripts/offline_test_worker.py'),str(payload),str(destination)],profile)
            completed=subprocess.run(command,cwd=root,env=environment,capture_output=True,text=True,timeout=120)
            if completed.stdout:print(completed.stdout,end='')
            if completed.stderr:print(completed.stderr,end='',file=sys.stderr)
            if not destination.is_file():
                raise ValueError('[incomplete] 离线 worker 未生成结果；系统隔离或启动失败，不能报告通过')
            python_result=json.loads(destination.read_text())
            if (completed.returncode==0)!=bool(python_result['ok']):
                raise ValueError('离线 worker 退出码与结果不一致')
        if plan['javascript']:
            command,backend=system_command(['node','--test',*plan['javascript']],profile)
            node=subprocess.run(command,cwd=root,env=environment,capture_output=True,text=True,timeout=120)
            if node.returncode:
                print(node.stdout)
                if node.stderr:print(node.stderr,file=sys.stderr)
        attempts_path=directory/'attempts.jsonl'
        attempts=[json.loads(line) for line in attempts_path.read_text().splitlines()] if attempts_path.exists() else []
        node_tests=re.search(r'^# tests (\d+)$',node.stdout,re.M) if node else None
        output={**plan,**python_result,'blockedConnectionAttempts':sum(item['kind']=='network' for item in attempts),
                'blockedFilesystemAttempts':sum(item['kind']=='filesystem' for item in attempts),
                'nodeExitCode':node.returncode if node else None,'nodeTests':int(node_tests[1]) if node_tests else 0,
                'offlineIsolation':{'backend':backend,'freshProcesses':True,'environmentCleared':True,'temporaryData':True,'descendantGuards':True},
                'ok':python_result['ok'] and not attempts and (node is None or node.returncode==0)}
    output['offlineIsolation']['temporaryDataRemoved']=not directory.exists()
    return output
