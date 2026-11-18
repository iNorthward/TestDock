#!/usr/bin/env python3
"""Build an independent platform and separately installable selected Pack bundles."""
from __future__ import annotations

import argparse
import fnmatch
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

from atomic_file import atomic_write_text
from check_neutral_distribution import findings
from platform_release import (
    DEMO_MANIFEST, DEMO_TEMPLATE, PRIVATE_PARTS, PRIVATE_SUFFIXES, RECEIPT, SOURCE_INVENTORY,
    declared_file, platform_relative_path,
    read_json, read_platform_api, read_source_inventory, sha256_file, verify_release,
)

ROOT = Path(__file__).resolve().parents[1]
CORE_INVENTORY = 'resources/platform-core-files.json'
REGRESSION_MANIFEST = 'resources/targeted-regression.json'
PRIVATE_BUNDLE_PARTS = PRIVATE_PARTS - {'adapters'}
SUPPORT_FILES = (
    'test-platform/document.html', 'test-platform/panel.html', 'test-platform/insights.html',
    'start.sh', 'stop.sh', 'scripts/load_env.sh', 'scripts/run_nightly.sh', 'scripts/offline_guard.cjs',
    CORE_INVENTORY, REGRESSION_MANIFEST, 'requirements.txt', '.nvmrc', '.env.example',
    'AGENTS.md', 'README.md', 'scripts/check.sh', 'platform_protocol.py',
    'tests/test_platform_distribution_runtime.py',
)


def _copy(source, destination, relative):
    origin = (source / relative).resolve()
    if not origin.is_relative_to(source) or not origin.is_file():
        raise ValueError(f"发行文件不存在或越界: {relative}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, target)
    target.chmod(origin.stat().st_mode & 0o777)


def _tree_files(source, relative):
    for path in sorted((source / relative).rglob('*')):
        relative_file = path.relative_to(source)
        if not _private_file(relative_file) and path.is_file():
            yield relative_file.as_posix()


def _private_file(relative):
    return (any(part.lower() in PRIVATE_BUNDLE_PARTS for part in relative.parts)
            or relative.suffix.lower() in PRIVATE_SUFFIXES
            or relative.name.lower().endswith('_pool.json')
            or any(part.startswith('.') and part != '.env.example' for part in relative.parts))


def _source_inputs(source):
    # Only Core, platform-owned support, and maintained neutral examples are authored here.
    regression = read_json(source / REGRESSION_MANIFEST)
    tests = {relative for relative in _tree_files(source, 'tests') if not Path(relative).name.startswith('test_')}
    for group in regression['groups'].values():
        tests.update('tests/' + name + '.py' for name in group.get('python', []))
        tests.update(group.get('javascript', []))
    return sorted(set([
        *read_json(source / CORE_INVENTORY), *SUPPORT_FILES, *tests,
        'test-platform/ui-scenes.html', 'test-platform/ui-scenes-demo.html',
        *_tree_files(source, 'assets'), *_tree_files(source, 'docs/architecture'),
        *_tree_files(source, 'docs/onboarding'), *_tree_files(source, 'docs/guides'),
        *_tree_files(source, 'docs/reports/mature-foundation'), *_tree_files(source, 'docs/reports/six-improvements'),
        *_tree_files(source, 'examples/demo_pack'), *_tree_files(source, 'examples/http_pack_template'),
        *_tree_files(source, 'examples/adapter_recipes'), *_tree_files(source, 'examples/workbench_components'),
        *_tree_files(source, 'examples/task_evidence'),
    ]))


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _source_inventory_document(files):
    return {'version': 1, 'files': dict(sorted(files.items()))}


def seal_source(source=ROOT):
    """Refresh reviewed inputs from the maintained neutral release source."""
    source = Path(source).resolve()
    if (source / RECEIPT).exists():
        raise ValueError('派生发行目录不能刷新中性源文件清单，请使用维护中的中性源码')
    inputs = _source_inputs(source)
    if DEMO_TEMPLATE not in inputs:
        raise ValueError('缺少独立 demo Pack 模板')
    files = {}
    for relative in inputs:
        platform_relative_path(relative)
        path = declared_file(source, relative)
        if path is None:
            raise ValueError(f'中性输入文件缺失: {relative}')
        files[relative] = sha256_file(path)
    path = source / SOURCE_INVENTORY
    if path.resolve() != path:
        raise ValueError('中性源文件清单不能使用符号链接')
    atomic_write_text(path, _json(_source_inventory_document(files)))
    return path


def _copy_verified(source, destination, files):
    for relative, saved_hash in files.items():
        origin = declared_file(source, relative)
        if origin is None:
            raise ValueError(f'中性输入文件缺失: {relative}')
        _copy(source, destination, relative)
        if sha256_file(destination / relative) != saved_hash:
            raise ValueError(
                f'中性输入文件摘要不匹配: {relative}。审阅后的中性源码可用 --seal-source 刷新；'
                '派生项目请指定原始 --neutral-source'
            )


def _neutral_platform(platform, source, neutral_source):
    if neutral_source is not None:
        files, api = verify_release(neutral_source)
        missing = {DEMO_MANIFEST, *SUPPORT_FILES} - files.keys()
        if missing:
            raise ValueError(f'原始平台发行缺少必要文件: {", ".join(sorted(missing))}')
        _copy_verified(neutral_source, platform, files)
    else:
        files = read_source_inventory(source)
        missing = {DEMO_TEMPLATE, *SUPPORT_FILES} - files.keys()
        if missing:
            raise ValueError(f'中性源文件清单缺少必要文件: {", ".join(sorted(missing))}')
        _copy_verified(source, platform, files)
        (platform / SOURCE_INVENTORY).write_text(_json(_source_inventory_document(files)), encoding='utf-8')
        demo = platform / DEMO_MANIFEST
        demo.parent.mkdir(parents=True)
        manifest = read_json(platform / DEMO_TEMPLATE)
        if not isinstance(manifest, dict) or manifest.get('id') != 'demo_pack' or manifest.get('default') is not True:
            raise ValueError('中性 demo 模板必须声明唯一默认 demo Pack')
        shutil.copyfile(platform / DEMO_TEMPLATE, demo)
        (platform / '.gitignore').write_text(
            '__pycache__/\n*.py[cod]\n.venv/\n.env*\n!.env.example\n.run/\n'
            'data/\nartifacts/\npacks/*/data/identity_pool.json\n*.db\n*.sqlite*\n.DS_Store\n',
            encoding='utf-8',
        )
        api = read_platform_api(platform / 'platform_protocol.py')
    problems = findings(platform)
    if problems:
        sample = ', '.join(f"{item['file']} ({item['kind']})" for item in problems[:5])
        raise ValueError(f'最终平台产物未通过中性检查: {sample}')
    return api


def _receipt(folder, kind, api=None):
    value = {'kind': kind, 'files': {path.relative_to(folder).as_posix(): sha256_file(path)
                                      for path in sorted(folder.rglob('*')) if path.is_file()},
             'credentialsIncluded': False, 'runtimeDataIncluded': False}
    if api is not None:
        value['platformApi'] = api
    (folder / RECEIPT).write_text(_json(value), encoding='utf-8')


def _copy_pack(source, platform, staging, pack, api):
    from platform_protocol import compatibility
    compatibility(pack, api_version=api['version'], available_features=set(api['features']))
    pack_id = pack['id']
    bundle = staging / ('pack-' + pack_id)
    bundle.mkdir()
    root = pack['root']
    folders = [root.relative_to(source).as_posix(), *pack.get('case_dirs', [])]
    files = {*pack.get('adapter_files', []), *pack.get('compatibility_files', [])}
    exclusions = pack.get('distribution_excludes', [])
    for folder in folders:
        for relative in _tree_files(source, folder):
            origin = source / relative
            local = origin.relative_to(root).as_posix() if origin.is_relative_to(root) else relative
            if not any(fnmatch.fnmatchcase(local, pattern) for pattern in exclusions):
                files.add(relative)
    for relative in sorted(files):
        # The neutral demo can also be bundled explicitly for a legacy caller.
        in_demo = pack_id == 'demo_pack' and relative.startswith('examples/demo_pack/')
        in_demo = in_demo or (pack_id == 'demo_pack' and relative == DEMO_MANIFEST)
        if ((platform / relative).exists() and not in_demo
                or relative.startswith(('resources/platform-', 'scripts/'))):
            raise ValueError(f'Pack 发行文件不得覆盖已登记平台文件: {relative}')
        if _private_file(Path(relative)) or declared_file(source, relative) is None:
            raise ValueError(f'Pack 发行路径包含私有文件或缺失文件: {relative}')
        _copy(source, bundle, relative)
    _receipt(bundle, 'pack-' + pack_id)


def _pack_bundles(source, platform, staging, pack_ids, api):
    # Inspect only the local source tree. An explicit neutral source is data, never imported.
    search_path = sys.path[:]
    sys.path[:0] = [str(source), str(source / 'test-platform')]
    try:
        spec = spec_from_file_location('platform_export_registry', source / 'test-platform/pack_registry.py')
        if spec is None or spec.loader is None:
            raise ValueError('无法读取本地 Pack 归属规则')
        registry = module_from_spec(spec)
        spec.loader.exec_module(registry)
        for pack_id in pack_ids:
            if not isinstance(pack_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', pack_id) or pack_id in {'.', '..'}:
                raise ValueError('Pack ID 无效')
            candidates = registry._read_candidates(pack_id)
            if len(candidates) != 1:
                raise ValueError(f'Pack 不存在或不唯一: {pack_id}')
            pack = registry._validate_manifest(candidates[0], candidates[0]['manifest_path'])
            _copy_pack(source, platform, staging, pack, api)
    finally:
        sys.path[:] = search_path


def build(destination, *, source=ROOT, pack_ids=(), neutral_source=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    reference = Path(neutral_source).resolve() if neutral_source is not None else None
    if (destination.exists() or source == destination or source.is_relative_to(destination)
            or reference is not None and reference.is_relative_to(destination)):
        raise ValueError('发行目标须为不存在的新目录，不能覆盖源码、原始发行或已有目录')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='platform-dist-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        platform = staging / 'platform'
        platform.mkdir()
        api = _neutral_platform(platform, source, reference)
        _receipt(platform, 'platform', api)
        if pack_ids:
            _pack_bundles(source, platform, staging, pack_ids, api)
        # Publish only after the final platform and all independently selected Packs pass.
        if destination.exists():
            raise ValueError('发行目标已存在')
        staging.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path)
    action.add_argument('--seal-source', action='store_true', help='维护者确认中性源码后刷新逐文件摘要')
    parser.add_argument('--pack', action='append', default=[])
    parser.add_argument('--neutral-source', type=Path, help='原始中性发行的 platform 目录')
    args = parser.parse_args()
    if args.seal_source and (args.pack or args.neutral_source):
        parser.error('--seal-source 不能与 --pack 或 --neutral-source 同时使用')
    try:
        if args.seal_source:
            print(seal_source())
        else:
            print(build(args.output, pack_ids=args.pack, neutral_source=args.neutral_source))
    except (ValueError, OSError, RuntimeError, SyntaxError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
