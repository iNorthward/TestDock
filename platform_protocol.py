"""Versioned platform/Pack extension contract, independent of business models."""
import re
API_VERSION = '1.0'
FEATURES = frozenset({'request_id', 'resource_pool', 'diagnostics', 'response_decoder', 'bounded_queue', 'history_compare', 'isolated_readonly'})

def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',value):
        raise ValueError('platform_api version must be major.minor')
    return tuple(int(part) for part in value.split('.'))

def compatibility(manifest):
    spec=manifest.get('platform_api')
    if spec is None:
        return {'version':API_VERSION,'compatible':True,'explicit':False,'features':[]}
    if not isinstance(spec,dict) or set(spec)-{'min','max_exclusive','features'}:
        raise ValueError('platform_api supports min/max_exclusive/features only')
    minimum=version(spec.get('min'));maximum=version(spec.get('max_exclusive'))
    if minimum>=maximum:raise ValueError('platform_api range must be increasing')
    requested=spec.get('features',[])
    if not isinstance(requested,list) or any(not isinstance(item,str) for item in requested) or len(set(requested))!=len(requested):
        raise ValueError('platform_api features must be unique strings')
    if not minimum<=version(API_VERSION)<maximum:
        raise ValueError('Pack requires an incompatible platform API range')
    missing=set(requested)-FEATURES
    if missing:raise ValueError('Pack requires unsupported platform features: '+', '.join(sorted(missing)))
    return {'version':API_VERSION,'compatible':True,'explicit':True,'features':list(requested)}

def default_requirement():
    return {'min':'1.0','max_exclusive':'2.0','features':['request_id']}
