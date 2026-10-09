#!/usr/bin/env python3
"""Generate an owned offline Pack; never overwrite an existing project."""
from __future__ import annotations
import argparse,json,keyword,re,shutil,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
STARTUP_ENV_KEYS={'PLATFORM_PACKS','PLATFORM_ENV_FILE','PLATFORM_PANEL_PORT',
                  'PLATFORM_PACK_DATA_DIR','PLATFORM_ARTIFACT_ROOT','TZ'}
def business_env_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*',value):
        raise ValueError('业务环境键须为显式大写标识符')
    if value in STARTUP_ENV_KEYS:
        raise ValueError('业务地址或认证不能使用平台启动配置键: '+value)
    return value

def pack_readme(pack_id,port,contract):
    smoke='HTTP-MOCK-R01' if contract else 'DEMO-R01'
    text=(f'# {pack_id} 接入说明\n\n'
          '所有命令从测试平台根目录运行。当前目录仅归所属业务项目所有；不修改 Core。\n\n'
          '## 离线验收\n\n```sh\n'
          f'python3 scripts/verify_project_pack.py --pack {pack_id} --mock-smoke --case-id {smoke}\n'
          f'PLATFORM_PACKS={pack_id} PLATFORM_ENV_FILE=/dev/null python3 scripts/targeted_regression.py --group {pack_id}.adapter\n'
          '```\n\n离线通过只说明装载、Adapter、案例及结果链路可用，不代表真实业务已经验证。\n\n')
    if contract:
        keys=list(dict.fromkeys([contract['base_env'],*(v['env'] for v in contract.get('headers',{}).values())]))
        text+=('## HTTP 配置\n\n'
               f'在忽略的 `.env.{pack_id}` 配置业务环境键：'+', '.join('`'+key+'`' for key in keys)+
               '。地址为当前服务 base URL，凭据使用本项目授权值；不要写入契约或源码。\n\n'
               '`http-contract.json` 保存已确认 GET 路径、状态、原始 JSON 类型断言与合成 mock。'
               '在界面单独运行 HTTP-R01；缺配置为 incomplete 且不发请求，401 或类型违约为 failed。'
               '复杂认证、响应信封、序列化和业务断言放本 Pack integration.py / helpers，注册案例在 cases/readonly_cases.py。\n\n')
    else:
        text+=('## 替换离线骨架\n\n'
               '当前 DEMO-R01 仅使用 transport.py 的本地 mock。真实认证与响应信封在 __init__.py 注册，'
               '请求和业务断言放本 Pack Adapter / helpers，新增案例在 cases/readonly_cases.py。'
               '没有确认的接口证据时保留 incomplete，不猜字段或默认账号。\n\n')
    text+=('## 独立启动与停止\n\n'
           'HTTP 项目先填写上面的配置；离线项目可直接启动。\n\n```sh\n'
           f'PLATFORM_ENV_FILE=.env.{pack_id} ./start.sh start\n'
           f'PLATFORM_ENV_FILE=.env.{pack_id} ./start.sh status\n'
           f'PLATFORM_ENV_FILE=.env.{pack_id} ./start.sh stop\n'
           f'```\n\n访问 http://localhost:{port}；历史和数据在 data/{pack_id}，证据在 artifacts/{pack_id}。'
           '端口冲突时只调整自己的环境文件，不停止其他项目。\n\n'
           '## 后续验收\n\n'
           '本项目 tests 和 regression.json 同步维护；默认资源池与高级诊断关闭，需要时由 pack.json 显式声明。'
           '凭据、历史和数据不提交。真实 GET 仍须确认无写副作用；写用例须独立授权、开关和清理。'
           '不执行全量业务案例。完整协议见平台根目录 docs/onboarding/AGENT_INTEGRATION.md。\n')
    return text

def http_contract(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('契约 JSON 包含重复键')
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('契约 JSON 不能包含非有限值')
    contract=json.loads(Path(path).read_text(),object_pairs_hook=pairs,parse_constant=invalid_constant)
    if not isinstance(contract,dict):raise ValueError('HTTP 契约必须是对象')
    if set(contract)-{'method','path','base_env','headers','expected_status','checks','mock_response'}:
        raise ValueError('HTTP 契约含未支持字段，凭据只使用环境键')
    if not isinstance(contract,dict) or contract.get('method')!='GET':raise ValueError('HTTP starter 只接入明确 GET 只读契约')
    endpoint=contract.get('path')
    if not isinstance(endpoint,str) or not endpoint.startswith('/') or endpoint.startswith('//') or any(c in endpoint for c in ('\r','\n','#')):
        raise ValueError('HTTP path 须为当前服务内绝对路径')
    business_env_key(contract.get('base_env'))
    if type(contract.get('expected_status')) is not int or not 100<=contract['expected_status']<=599:raise ValueError('expected_status 无效')
    checks=contract.get('checks')
    if not isinstance(checks,list) or not checks:raise ValueError('须提供显式响应 checks')
    for check in checks:
        if (not isinstance(check,dict) or set(check)-{'path','type','equals'} or check.get('type') not in {'string','boolean','integer','number','object','array','null'}
                or not isinstance(check.get('path'),list) or not check['path']
                or any(not isinstance(v,str) and (type(v) is not int or v<0) for v in check['path'])):
            raise ValueError('响应 check path/type 无效')
    headers=contract.get('headers',{})
    if not isinstance(headers,dict):raise ValueError('headers 须为对象')
    for key,spec in headers.items():
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",key) or not isinstance(spec,dict)
                or set(spec)-{'env','prefix'} or not re.fullmatch(r'[A-Z][A-Z0-9_]*',str(spec.get('env','')))
                or not isinstance(spec.get('prefix',''),str) or any(v in spec.get('prefix','') for v in ('\r','\n'))):
            raise ValueError('headers 只允许显式 env/prefix，不写入真实凭据')
        business_env_key(spec['env'])
    mock=contract.get('mock_response')
    if not isinstance(mock,dict) or type(mock.get('status')) is not int or 'body' not in mock:raise ValueError('须提供显式 mock_response')
    return contract

def create(pack_id,name,port,*,root=ROOT,contract_file=None):
    contract=http_contract(contract_file) if contract_file else None
    root=Path(root).resolve()
    if not re.fullmatch(r'[a-z][a-z0-9_]*',pack_id) or pack_id=='demo_pack' or keyword.iskeyword(pack_id):
        raise ValueError('id 须为小写 Python 标识符，不能覆盖 demo_pack')
    if not isinstance(name,str) or not name.strip(): raise ValueError('name 不能为空')
    if type(port) is not int or not 1<=port<=65535: raise ValueError('port 必须为 1–65535 的整数')
    target=root/'packs'/pack_id;env=root/('.env.'+pack_id)
    if target.exists() or env.exists(): raise ValueError('Pack 或环境文件已存在；拒绝覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.new-pack-',dir=target.parent) as tmp:
        stage=Path(tmp)/pack_id;stage.mkdir();(stage/'cases').mkdir();(stage/'cases/__init__.py').write_text('')
        source=root/'examples/demo_pack'
        for filename in ('__init__.py','transport.py'):
            shutil.copyfile(source/filename,stage/filename)
        text=(source/'readonly_cases.py').read_text().replace('examples.demo_pack','packs.'+pack_id)
        (stage/'cases/readonly_cases.py').write_text(text)
        manifest={'platform_api':{'min':'1.0','max_exclusive':'2.0','features':['request_id']},'id':pack_id,'display_name':name,'default':False,'bootstrap':'packs.'+pack_id+':install',
            'case_dirs':[str(Path('packs')/pack_id/'cases')],'navigation':[{'id':'demo','name':'离线接入验收','parents':['demo']}],
            'regression_manifest':str(Path('packs')/pack_id/'regression.json'),'identity_pool_file':'data/identity_pool.json','identity_requirements':{},'resource_pool':{'enabled':False},
            'config_defaults':{'PLATFORM_PACK_DATA_DIR':'data/'+pack_id,'PLATFORM_ARTIFACT_ROOT':'artifacts/'+pack_id,'PLATFORM_PANEL_PORT':str(port)}}
        (stage/'pack.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        (stage/'tests').mkdir();(stage/'tests/__init__.py').write_text('')
        test_code=("import unittest\nfrom packs."+pack_id+".transport import MockTransport\n\n"
                   "class AdapterContractTests(unittest.TestCase):\n"
                   "    def test_offline_health_contract(self):\n"
                   "        transport=MockTransport()\n"
                   "        status,body=transport.request('GET','https://offline.invalid/health',{})\n"
                   "        self.assertEqual(status,200)\n"
                   "        self.assertIs(body['ok'],True)\n"
                   "        self.assertEqual(body['data']['status'],'ready')\n"
                   "        self.assertEqual(len(transport.calls),1)\n"
                   "    def test_unknown_route_rejects(self):\n"
                   "        status,body=MockTransport().request('GET','https://offline.invalid/unknown',{})\n"
                   "        self.assertEqual(status,404)\n"
                   "        self.assertIs(body['ok'],False)\n")
        (stage/'tests/test_adapter.py').write_text(test_code)
        regression={'version':1,'groups':{pack_id+'.adapter':{'sources':[str(Path('packs')/pack_id/'**/*.py'),str(Path('packs')/pack_id/'pack.json')],
                    'python':['packs.'+pack_id+'.tests.test_adapter'],'javascript':[]}}}
        (stage/'regression.json').write_text(json.dumps(regression,ensure_ascii=False,indent=2)+'\n')
        (stage/'README.md').write_text(pack_readme(pack_id,port,contract))
        if contract:
            template=root/'examples/http_pack_template'
            for filename in ('__init__.py','integration.py'):
                shutil.copyfile(template/filename,stage/filename)
            (stage/'transport.py').unlink()
            (stage/'cases/readonly_cases.py').write_text((template/'readonly_cases.py').read_text().replace('PACK_IDENTIFIER',pack_id))
            (stage/'tests/test_adapter.py').write_text((template/'test_adapter.py').read_text().replace('PACK_IDENTIFIER',pack_id))
            (stage/'http-contract.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2)+'\n')
            contract_key=pack_id.upper()+'_HTTP_CONTRACT_FILE'
            manifest['required_environment']=list(dict.fromkeys([contract['base_env'],*(v['env'] for v in contract.get('headers',{}).values())]))
            manifest['execution_contract_sources']={'http':contract_key}
            manifest['config_defaults'][contract_key]=str(Path('packs')/pack_id/'http-contract.json')
            manifest['navigation']=[{'id':'http','name':'HTTP 接入','parents':['http']}]
            (stage/'pack.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
            regression['groups'][pack_id+'.adapter']['sources'].append(str(Path('packs')/pack_id/'http-contract.json'))
            (stage/'regression.json').write_text(json.dumps(regression,ensure_ascii=False,indent=2)+'\n')
        stage.rename(target)
    try:
        with env.open('x') as f:f.write('PLATFORM_PACKS='+pack_id+'\nPLATFORM_PANEL_PORT='+str(port)+'\nPLATFORM_PACK_DATA_DIR=data/'+pack_id+'\nPLATFORM_ARTIFACT_ROOT=artifacts/'+pack_id+'\nTZ=Asia/Shanghai\n')
        if contract:
            with env.open('a') as f:
                for key in dict.fromkeys([contract['base_env'],*(spec['env'] for spec in contract.get('headers',{}).values())]):
                    f.write(key+'=\n')
        env.chmod(0o600)
    except BaseException:
        shutil.rmtree(target)
        raise
    return target
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--id',required=True);p.add_argument('--name',required=True);p.add_argument('--port',type=int,required=True);p.add_argument('--http-contract');a=p.parse_args()
    try:
        target=create(a.id,a.name,a.port,contract_file=a.http_contract);print('Created:',target.relative_to(ROOT));print('Verify: python3 scripts/verify_project_pack.py --pack '+a.id+' --mock-smoke'+(' --case-id HTTP-MOCK-R01' if a.http_contract else ''))
    except (ValueError,OSError) as e:p.exit(2,str(e)+'\n')
