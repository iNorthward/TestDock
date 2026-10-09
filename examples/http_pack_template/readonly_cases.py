"""Offline and real HTTP SAFE probes; live addresses are never implicit."""
from packs.PACK_IDENTIFIER.integration import CONTRACT,run_probe
SUITES=[{'id':'http-readonly','name':'HTTP 接入验收','parent':'http','endpoint':'显式契约中的 GET','account':'项目配置','hasCleanup':False}]
CATALOG=[{'id':'HTTP-MOCK-R01','suite':'http-readonly','group':'HTTP','desc':'显式契约离线验收','mode':'safe','expect':'mock','api':[],'expected':'原始响应类型与值匹配'},
         {'id':'HTTP-R01','suite':'http-readonly','group':'HTTP','desc':'项目 HTTP 只读验收','mode':'safe','expect':'read','api':[],'expected':'HTTP 状态与显式契约一致；缺配置返回 incomplete'}]
SUITES[0]['endpoint']='GET '+CONTRACT['path']
CATALOG[1]['api']=['GET '+CONTRACT['path']]
CAT={case['id']:case for case in CATALOG}
RUNNERS={'mock':lambda case:run_probe(mock=True),'read':lambda case:run_probe()}
RUNNER_API={'mock':[],'read':['GET '+CONTRACT['path']]}
def exec_case(case_id):
    case=next((c for c in CATALOG if c['id']==case_id),None)
    if not case:return {'id':case_id,'ok':False,'status':'failed','error':'未知案例'}
    result=RUNNERS[case['expect']](case);result['id']=case_id;return result
