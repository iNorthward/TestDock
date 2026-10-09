#!/usr/bin/env python3
"""One-command selected Pack health report with explicit next actions, no secrets."""
from __future__ import annotations
import argparse,json,re,socket,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from platform_protocol import compatibility
from timezone_config import parse_env_value
from verify_project_pack import verify

def environment(path):
    result={}
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        key,_,value=line.partition('=');key=key.strip()
        if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',key):result[key]=parse_env_value(value)
    return result

def diagnose(pack_id,*,root=ROOT,env_file=None,mock_case=None,probe_port=False):
    root=Path(root).resolve()
    if not re.fullmatch('[a-z][a-z0-9_]*',pack_id):raise ValueError('invalid pack id')
    checks=[]
    def add(name,status,detail,action=''):
        checks.append({'name':name,'status':status,'detail':detail,'nextAction':action})
    folder=root/'packs'/pack_id;manifest_path=folder/'pack.json'
    if not manifest_path.is_file() or not manifest_path.resolve().is_relative_to(root/'packs'):
        add('manifest','failed','Missing or escaping selected Pack','Generate an owned Pack with create_project_pack.py')
        return {'pack':pack_id,'ok':False,'status':'failed','checks':checks,'realBusinessVerified':False}
    manifest=json.loads(manifest_path.read_text())
    try:
        protocol=compatibility(manifest)
        add('platform-api','passed' if protocol['explicit'] else 'warning','Compatible API '+protocol['version'],
            '' if protocol['explicit'] else 'Declare platform_api min/max_exclusive/features in pack.json')
    except ValueError as exc:add('platform-api','failed',str(exc),'Update the Pack or use a compatible platform release')
    default_env=root/('.env.'+pack_id)
    if not default_env.exists() and manifest.get('default') is True:default_env=root/'.env.local'
    env_path=Path(env_file) if env_file else default_env
    if not env_path.is_absolute():env_path=root/env_path
    values={}
    if env_path.is_file():
        values=environment(env_path)
        add('environment','passed' if values.get('PLATFORM_PACKS')==pack_id else 'failed','Selected environment matches Pack' if values.get('PLATFORM_PACKS')==pack_id else 'Environment selects another Pack','Set PLATFORM_PACKS to '+pack_id)
    else:add('environment','warning','No project environment file','Create .env.'+pack_id+' with separate port/data/artifacts')
    defaults=manifest.get('config_defaults',{})
    if not isinstance(defaults,dict):
        add('manifest','failed','config_defaults must be an object','Fix selected Pack manifest')
        defaults={}
    for key in ('PLATFORM_PACK_DATA_DIR','PLATFORM_ARTIFACT_ROOT'):
        value=values.get(key,defaults.get(key,''))
        add(key,'passed' if value else 'incomplete','Project path declared' if value else 'Missing project path','Declare '+key+' for this project')
    required=manifest.get('required_environment',[])
    if not isinstance(required,list) or any(not isinstance(k,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',k) for k in required):
        add('manifest','failed','required_environment must be an array of uppercase key names','Fix selected Pack manifest')
        required=[]
    if required:
        missing=[key for key in required if not values.get(key,'').strip()]
        add('declared-environment','incomplete' if missing else 'passed','Missing keys: '+', '.join(missing) if missing else 'Declared key names present; values hidden','Configure only this project private environment')
    recipe=folder/'http-contract.json'
    if recipe.is_file():
        contract=json.loads(recipe.read_text());keys=list(dict.fromkeys([contract['base_env'],*(v['env'] for v in contract.get('headers',{}).values())]))
        missing=[key for key in keys if not values.get(key,'').strip()]
        add('business-configuration','incomplete' if missing else 'passed','Missing keys: '+', '.join(missing) if missing else 'Required key names present; values hidden','Fill these keys in the selected private environment file; never in source')
    port=str(values.get('PLATFORM_PANEL_PORT',defaults.get('PLATFORM_PANEL_PORT','')))
    if not port.isascii() or not port.isdecimal() or not 1<=int(port)<=65535:
        add('port','failed','Invalid project port','Set PLATFORM_PANEL_PORT to a decimal port from 1 to 65535')
    elif probe_port:
        try:
            with socket.socket() as stream:stream.bind(('127.0.0.1',int(port)))
            add('port','passed','Port available')
        except OSError:add('port','warning','Port occupied or unavailable','Use this environment with ./start.sh status; otherwise choose another port')
    else:add('port','passed','Port configuration valid; availability not probed')
    try:
        result=verify(pack_id,root=root,smoke=mock_case)
        add('catalog-adapter','passed','Catalog and bootstrap load in a clean offline process')
        if mock_case:add('mock-smoke','passed','Selected offline SAFE case passed')
        else:add('mock-smoke','not_run','No explicit mock case supplied','Repeat with --mock-case <owned offline SAFE id>')
    except (ValueError,RuntimeError) as exc:
        # A trusted bootstrap may include configured values in its own exception.
        detail=str(exc).replace(str(root),'[platform]')
        for value in sorted((str(v) for v in values.values() if len(str(v))>=4),key=len,reverse=True):detail=detail.replace(value,'[redacted]')
        add('catalog-adapter','failed',detail[:1200],'Fix owned manifest/bootstrap/case errors, then rerun the doctor')
    statuses={item['status'] for item in checks}
    status='failed' if 'failed' in statuses else 'incomplete' if 'incomplete' in statuses else 'ready'
    return {'pack':pack_id,'ok':status=='ready','status':status,'checks':checks,'realBusinessVerified':False,
            'nextCommand':'PLATFORM_ENV_FILE=.env.'+pack_id+' ./start.sh start'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pack',required=True);p.add_argument('--env-file');p.add_argument('--mock-case');p.add_argument('--probe-port',action='store_true');p.add_argument('--report',type=Path);a=p.parse_args()
    try:
        d=diagnose(a.pack,env_file=a.env_file,mock_case=a.mock_case,probe_port=a.probe_port)
        if a.report:a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(d,ensure_ascii=False,indent=2));return 0 if d['ok'] else 2
    except (ValueError,OSError,KeyError) as exc:p.exit(2,str(exc)+'\n')
if __name__=='__main__':raise SystemExit(main())
