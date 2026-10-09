"""Project-owned HTTP checks from an explicit contract, without field guessing."""
from __future__ import annotations
import json,math,os
from pathlib import Path
from urllib.parse import urlsplit
from api_client import ApiSession
CONTRACT=json.loads(Path(__file__).with_name('http-contract.json').read_text())
class ContractHeaders:
    def build_headers(self,token=None,**options):
        result={'Accept':'application/json'}
        for name,spec in CONTRACT.get('headers',{}).items():
            value=os.environ.get(spec['env'],'')
            if not value:raise ValueError('缺少认证配置键 '+spec['env'])
            result[name]=spec.get('prefix','')+value
        return result
    def validate_headers(self,headers):
        if any(not isinstance(v,str) or '\r' in v or '\n' in v for v in headers.values()):
            raise ValueError('header 配置包含非法值')

def value_at(body,path):
    value=body
    for part in path:
        if isinstance(part,str) and isinstance(value,dict) and part in value:value=value[part]
        elif type(part) is int and isinstance(value,list) and 0<=part<len(value):value=value[part]
        else:raise ValueError('响应字段缺失或容器类型错误')
    return value

def matches_type(value,kind):
    return {'string':isinstance(value,str),'boolean':type(value) is bool,'integer':type(value) is int,
            'number':type(value) in (int,float) and (type(value) is int or math.isfinite(value)),
            'object':isinstance(value,dict),'array':isinstance(value,list),'null':value is None}[kind]

def inspect_response(status,body):
    failures=[]
    if status!=CONTRACT['expected_status']:failures.append('HTTP 状态不匹配')
    for check in CONTRACT['checks']:
        try:
            value=value_at(body,check['path'])
            if not matches_type(value,check['type']):failures.append('原始 JSON 类型不匹配: '+str(check['path']))
            elif 'equals' in check and (not matches_type(check['equals'],check['type']) or value!=check['equals']):
                failures.append('响应值不匹配: '+str(check['path']))
        except ValueError as error:failures.append(str(error)+': '+str(check['path']))
    return failures

class ContractMockTransport:
    def request(self,method,url,headers,*,data=None,timeout=30):
        response=CONTRACT['mock_response']
        return response['status'],response['body']

def run_probe(*,mock=False):
    keys=[spec['env'] for spec in CONTRACT.get('headers',{}).values()]
    if not mock and (not os.environ.get(CONTRACT['base_env']) or any(not os.environ.get(k) for k in keys)):
        return {'ok':None,'status':'incomplete','subs':[{'label':'配置','ok':None,'status':'incomplete','detail':'缺少当前项目的服务地址或认证配置；未发请求'}]}
    try:
        if not mock:
            base=urlsplit(os.environ[CONTRACT['base_env']])
            if base.scheme not in ('http','https') or not base.netloc or base.username or base.password or base.query or base.fragment:
                return {'ok':None,'status':'incomplete','subs':[{'label':'服务地址','ok':None,'status':'incomplete','detail':'服务地址必须为不含凭据的 HTTP(S) base URL；未发请求'}]}
        # Mock uses explicit synthetic headers, not real credentials.
        headers=ContractHeaders() if not mock else MockHeaders()
        session=ApiSession(tenant='',base='https://offline.invalid' if mock else os.environ[CONTRACT['base_env']],
                           request_auth=headers,transport=ContractMockTransport() if mock else None)
        status,body=session.get(CONTRACT['path'],with_token=False)
        failures=inspect_response(status,body)
        return {'ok':not failures,'status':'failed' if failures else 'passed',
                'subs':[{'label':'HTTP 只读探针','ok':not failures,'detail':'; '.join(failures) if failures else '显式 HTTP 状态、原始 JSON 类型与字段值均匹配'}]}
    except Exception as error:
        return {'ok':False,'status':'failed','subs':[{'label':'HTTP 只读探针','ok':False,'detail':'请求或配置失败: '+type(error).__name__}]}

class MockHeaders:
    def build_headers(self,token=None,**options):return {'Accept':'application/json'}
    def validate_headers(self,headers):pass
