"""Project-owned resource taxonomy and optional pool capability."""
from __future__ import annotations
import re

_ID = re.compile(r'^[a-z][a-z0-9_]*$')
_RESERVED = {'scenario', 'scenario_fields', 'scenario_refs', 'notes', 'distinct_roles', 'fixture_templates'}

def valid_category(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))

def resource_pool_definition(pack):
    raw = pack.get('resource_pool')
    if raw is None:
        # Read old explicit requirements, without inventing project categories.
        requirements = pack.get('identity_requirements') or {}
        categories = [{'id': key, 'label': key, 'storage': 'identities'}
                      for key, value in requirements.items()
                      if key not in _RESERVED and isinstance(value, list) and valid_category(key)]
        raw = {'enabled': bool(categories or requirements.get('scenario')), 'categories': categories}
    if not isinstance(raw, dict) or type(raw.get('enabled', False)) is not bool:
        raise ValueError('resource_pool.enabled 必须是布尔值')
    categories = raw.get('categories', [])
    if not isinstance(categories, list):
        raise ValueError('resource_pool.categories 必须是数组')
    seen = set()
    result = []
    for item in categories:
        if not isinstance(item, dict) or not valid_category(item.get('id')) or item['id'] in seen:
            raise ValueError('resource_pool 分类需要唯一的合法 id')
        seen.add(item['id'])
        storage = item.get('storage', 'identities')
        if storage == 'identities' and item['id'] in {'scenario', 'fixture'}:
            raise ValueError('资源分类 id 与集合名称冲突')
        if storage not in {'identities', 'scenarios', 'fixtures'}:
            raise ValueError('resource_pool 分类 storage 无效')
        if storage != 'identities' and any(c['storage'] == storage for c in result):
            raise ValueError('resource_pool 场景或夹具 storage 不得重复')
        label = item.get('label', item['id'])
        if not isinstance(label, str) or not label.strip():
            raise ValueError('resource_pool 分类 label 必须为非空字符串')
        fields = item.get('display_fields', [])
        if not isinstance(fields, list) or any(not isinstance(f, str) or not f.strip() for f in fields):
            raise ValueError('resource_pool display_fields 必须是字符串数组')
        result.append({**item, 'storage': storage, 'label': label, 'display_fields': fields})
    if not raw.get('enabled', False) and result:
        raise ValueError('resource_pool 关闭时不得声明分类')
    for key in ('title', 'description'):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError('resource_pool 标题和说明必须是字符串')
    relations = raw.get('relationship_fields', [])
    if not isinstance(relations, list) or any(not isinstance(f, str) or not f.strip() for f in relations):
        raise ValueError('resource_pool relationship_fields 必须是字符串数组')
    return {'enabled': raw.get('enabled', False), 'title': raw.get('title', '项目资源'),
            'description': raw.get('description', '当前项目声明的测试资源与静态维护建议。'),
            'categories': result, 'relationship_fields': relations}

def identity_categories(pack):
    return [c['id'] for c in resource_pool_definition(pack)['categories'] if c['storage'] == 'identities']

def selected_definition(pack_id=None):
    from pack_registry import load_packs
    pack = load_packs()[0]
    if pack_id and pack_id != pack['id']:
        raise ValueError('请求的资源池不属于当前项目 Pack')
    return resource_pool_definition(pack)
