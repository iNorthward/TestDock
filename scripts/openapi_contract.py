"""Platform-only OpenAPI normalization; never infer project business policy.

Conservative candidate parser, not a full JSON Schema validator. Unsupported or
conflicting composition is explicit rather than silently becoming an empty object.
"""
from __future__ import annotations

from copy import deepcopy

HTTP_METHODS = frozenset({'get', 'post', 'put', 'delete', 'patch', 'head', 'options'})
UNRESOLVED = 'x-platform-unresolved'


def unresolved(reason):
    return {UNRESOLVED: reason}


def merge_constraints(left, right):
    result = deepcopy(left)
    for key, value in right.items():
        if key not in result or result[key] == value:
            result[key] = deepcopy(value)
        elif key == 'required' and isinstance(result[key], list) and isinstance(value, list):
            result[key] = list(dict.fromkeys(result[key] + value))
        elif key == 'properties' and isinstance(result[key], dict) and isinstance(value, dict):
            for name, field in value.items():
                result[key][name] = merge_constraints(result[key].get(name, {}), field)
        elif key in {'description', 'title', 'example', 'examples'}:
            result[key] = deepcopy(value)
        else:
            return unresolved(f'组合约束 {key} 冲突或暂不支持，需确认完整契约')
    return result


class OpenAPIContract:
    def __init__(self, document):
        self.document = document

    def resolve(self, value, seen=(), depth=0):
        if depth > 40:
            return unresolved('引用展开超过深度限制')
        if isinstance(value, list):
            return [self.resolve(item, seen, depth + 1) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        if '$ref' in value:
            ref = value['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/'):
                return unresolved(f'不支持外部引用: {ref}')
            if ref in seen:
                return unresolved(f'循环引用: {ref}')
            target = self.document
            try:
                for part in ref[2:].split('/'):
                    target = target[part.replace('~1', '/').replace('~0', '~')]
            except (KeyError, TypeError):
                return unresolved(f'引用不存在: {ref}')
            if not isinstance(target, dict):
                return unresolved(f'引用目标不是对象: {ref}')
            resolved = self.resolve(target, seen + (ref,), depth + 1)
            siblings = self.resolve({k: v for k, v in value.items() if k != '$ref'}, seen, depth + 1)
            return merge_constraints(resolved, siblings)
        result = {}
        for key, child in value.items():
            if key in ('properties', 'content', 'responses') and isinstance(child, dict):
                result[key] = {name: self.resolve(entry, seen, depth + 1)
                               for name, entry in child.items()}
            elif key in ('items', 'additionalProperties', 'schema', 'parameters', 'requestBody', 'allOf', 'oneOf', 'anyOf'):
                result[key] = self.resolve(child, seen, depth + 1)
            else:
                result[key] = deepcopy(child)
        for keyword in ('oneOf', 'anyOf'):
            if keyword in result:
                parts = result[keyword]
                if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) for p in parts):
                    return unresolved(f'{keyword} 必须是非空 schema 对象数组')
        if 'allOf' in result:
            parts = result.pop('allOf')
            if not isinstance(parts, list) or any(not isinstance(p, dict) for p in parts):
                return unresolved('allOf 必须是 schema 对象数组')
            for part in parts:
                result = merge_constraints(result, part)
        return result

    def operations(self, prefix=''):
        for path, raw_item in self.document.get('paths', {}).items():
            if not isinstance(path, str) or not path.startswith('/') or not path.startswith(prefix):
                continue
            if not isinstance(raw_item, dict):
                continue
            item = self.resolve(raw_item)
            if UNRESOLVED in item:
                raise ValueError(f'{path}: {item[UNRESOLVED]}')
            for method, raw_spec in item.items():
                if not isinstance(method, str) or method.lower() not in HTTP_METHODS or not isinstance(raw_spec, dict):
                    continue
                spec = self.resolve(raw_spec)
                params = {}
                inherited = self.resolve(item.get('parameters') or [])
                declared = spec.get('parameters') or []
                if not isinstance(inherited, list) or not isinstance(declared, list):
                    raise ValueError(f'{method.upper()} {path}: parameters 必须是数组')
                for param in inherited + declared:
                    if not isinstance(param, dict) or not isinstance(param.get('name'), str) or not param.get('name') or param.get('in') not in {'query', 'path', 'header', 'cookie'}:
                        raise ValueError(f'{method.upper()} {path}: 无法解析 parameter: {param}')
                    params[(param['in'], param['name'])] = param
                spec['parameters'] = list(params.values())
                spec['x-platform-path-methods'] = [m.upper() for m, op in item.items() if isinstance(m, str) and m.lower() in HTTP_METHODS and isinstance(op, dict)]
                if 'security' not in spec and 'security' in self.document:
                    spec['security'] = deepcopy(self.document['security'])
                yield method.upper(), path, spec


def resolve_schema(schema, document=None):
    result = OpenAPIContract(document or {}).resolve(schema)
    return result if isinstance(result, dict) else unresolved('schema 不是对象')


def schema_issues(value):
    if isinstance(value, dict):
        issues = [value[UNRESOLVED]] if UNRESOLVED in value else []
        return issues + [issue for child in value.values() for issue in schema_issues(child)]
    if isinstance(value, list):
        return [issue for child in value for issue in schema_issues(child)]
    return []


def schema_type(schema):
    """Return a single structural type, retaining nullable union in the schema."""
    if 'oneOf' in schema or 'anyOf' in schema:
        return 'union'
    declared = schema.get('type')
    if isinstance(declared, list):
        non_null = [item for item in declared if item != 'null']
        return non_null[0] if len(non_null) == 1 else None
    return declared
