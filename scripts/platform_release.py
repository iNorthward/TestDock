"""Read platform file inventories without importing code from an external release."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re


SOURCE_INVENTORY = 'resources/platform-distribution-files.json'
RECEIPT = 'distribution.json'
DEMO_MANIFEST = '/'.join(('packs', 'demo_pack', 'pack.json'))
DEMO_TEMPLATE = 'examples/demo_pack/pack.json'
ROOT_FILES = {'.env.example', '.gitignore', '.nvmrc', 'AGENTS.md', 'README.md',
              'platform_config.py', 'platform_protocol.py', 'platform_capabilities.py',
              'timezone_config.py', 'requirements.txt', 'start.sh', 'stop.sh'}
PLATFORM_DIRS = {'assets', 'docs', 'examples', 'resources', 'scripts', 'test-platform', 'tests'}
PRIVATE_PARTS = {'.git', '.idea', '.run', '.venv', '.execution-locks', 'data', 'artifacts', 'adapters',
                 '__pycache__', 'node_modules', '.pytest_cache'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pyc', '.log'}


def _unique_object(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError('JSON 含重复键')
        values[key] = value
    return values


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)


def platform_relative_path(relative):
    """Permit neutral relative paths and the one generated demo manifest."""
    if not isinstance(relative, str) or any(ord(char) < 32 for char in relative):
        raise ValueError('平台文件清单含非法路径')
    parts = relative.split('/')
    private = any(part.lower() in PRIVATE_PARTS or
                  (part.startswith('.') and part not in {'.gitignore', '.env.example', '.nvmrc'})
                  for part in parts)
    owned = relative == DEMO_MANIFEST or relative in ROOT_FILES or (len(parts) > 1 and parts[0] in PLATFORM_DIRS)
    if (not relative or '\\' in relative or ':' in relative or private or
            any(part in {'', '.', '..'} for part in parts) or not owned or
            Path(relative).suffix.lower() in PRIVATE_SUFFIXES or parts[-1].lower().endswith('_pool.json') or
            relative == RECEIPT):
        raise ValueError('平台文件清单含运行数据、项目资产或非相对路径')
    return relative


def declared_file(root, relative):
    """Resolve a declared file without crossing a symlink or opening a special file."""
    path = root / relative
    if path.resolve() != path:
        raise ValueError(f'平台文件路径不能使用符号链接: {relative}')
    if path.is_file():
        return path
    if path.exists():
        raise ValueError(f'平台文件不是普通文件: {relative}')
    return None


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_map(values):
    if not isinstance(values, dict) or not values:
        raise ValueError('平台文件清单必须是非空的路径与 SHA-256 对象')
    names = set()
    for relative, digest in values.items():
        platform_relative_path(relative)
        if relative.casefold() in names:
            raise ValueError('平台文件清单含大小写冲突')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError(f'平台文件的 SHA-256 无效: {relative}')
        names.add(relative.casefold())
    return values


def read_source_inventory(root):
    path = declared_file(root, SOURCE_INVENTORY)
    if path is None:
        raise ValueError('缺少中性源文件清单，请指定已核验的 --neutral-source')
    value = read_json(path)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('不支持的中性源文件清单版本')
    files = _hash_map(value.get('files'))
    if {SOURCE_INVENTORY, '.gitignore', DEMO_MANIFEST}.intersection(files):
        raise ValueError('中性源文件清单不能包含生成文件')
    return files


def platform_api(value):
    if not isinstance(value, dict) or set(value) != {'version', 'features'}:
        raise ValueError('发行的 platformApi 须含 version 和 features')
    api_version, features = value['version'], value['features']
    if not isinstance(api_version, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', api_version):
        raise ValueError('发行的 API 版本必须为 major.minor')
    if (not isinstance(features, list) or any(not isinstance(item, str) or not item.strip() for item in features)
            or len(features) != len(set(features))):
        raise ValueError('发行的 API 能力须为无重复的字符串列表')
    return {'version': api_version, 'features': sorted(features)}


def read_platform_api(path, *, expected_digest=None):
    """Read the static platform declaration used by legacy and current receipts."""
    content = path.read_bytes()
    if expected_digest is not None and hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError('原始发行文件摘要不匹配: platform_protocol.py')
    values = {}
    for node in ast.parse(content.decode('utf-8')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name not in {'API_VERSION', 'FEATURES'}:
                continue
            if name in values:
                raise ValueError('发行的静态平台协议声明重复')
            value = node.value
            if (name == 'FEATURES' and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in {'set', 'frozenset'} and len(value.args) == 1 and not value.keywords):
                value = value.args[0]
            try:
                values[name] = ast.literal_eval(value)
            except ValueError as exc:
                raise ValueError('发行的静态平台协议声明无法解析') from exc
    features = values.get('FEATURES')
    if isinstance(features, (set, tuple, list)):
        features = list(features)
    return platform_api({'version': values.get('API_VERSION'), 'features': features})


def read_receipt(path):
    value = read_json(Path(path))
    if (not isinstance(value, dict) or value.get('kind') != 'platform'
            or value.get('credentialsIncluded') is not False or value.get('runtimeDataIncluded') is not False):
        raise ValueError('需要平台发行收据，且不得包含凭据或运行数据')
    files = _hash_map(value.get('files'))
    api = platform_api(value['platformApi']) if 'platformApi' in value else None
    return files, api


def verify_release(root):
    """Check original receipt entries and bind the advertised API to shipped code."""
    receipt = declared_file(root, RECEIPT)
    if receipt is None:
        raise ValueError('缺少原始平台 distribution.json')
    files, api = read_receipt(receipt)
    if 'platform_protocol.py' not in files:
        raise ValueError('发行收据未登记平台协议文件')
    protocol = None
    for relative, expected in files.items():
        path = declared_file(root, relative)
        if path is None:
            raise ValueError(f'原始发行文件缺失: {relative}')
        if relative == 'platform_protocol.py':
            protocol = read_platform_api(path, expected_digest=expected)
        elif sha256_file(path) != expected:
            raise ValueError(f'原始发行文件摘要不匹配: {relative}')
    if api is not None and api != protocol:
        raise ValueError('发行收据的 API 能力与协议文件不一致')
    return files, protocol
