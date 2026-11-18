#!/usr/bin/env python3
"""Compare a project checkout with an original and a target platform release."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    # A preflight from the current checkout must not populate its bytecode cache.
    sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from platform_protocol import compatibility
from platform_release import (
    PRIVATE_PARTS, RECEIPT, SOURCE_INVENTORY, declared_file, platform_relative_path,
    read_json, read_receipt, read_source_inventory, sha256_file, verify_release,
)

CORE_INVENTORY = 'resources/platform-core-files.json'


def _registered_paths(current):
    paths = set()
    core = declared_file(current, CORE_INVENTORY)
    if core is not None:
        names = read_json(core)
        if not isinstance(names, list):
            raise ValueError('当前 Core 清单必须是路径数组')
        paths.update(platform_relative_path(relative) for relative in names)
    if declared_file(current, SOURCE_INVENTORY) is not None:
        paths.update(read_source_inventory(current))
    return paths


def _change(before, after):
    if before == after:
        return 'unchanged'
    if before is None:
        return 'added'
    if after is None:
        return 'removed'
    return 'modified'


def _compare(path, base, local, target, baseline_known):
    if not baseline_known:
        source = 'matches_target' if local == target else 'unknown_origin'
    elif local == target:
        source = 'unchanged' if local == base else 'same_change'
    elif local == base:
        source = 'upstream_only'
    elif target == base:
        source = 'local_only'
    else:
        source = 'conflict'
    return {
        'file': path,
        'classification': source,
        'baselineSha256': base,
        'currentSha256': local,
        'targetSha256': target,
        'localChange': _change(base, local) if baseline_known else 'unknown',
        'targetChange': _change(base, target) if baseline_known else 'unknown',
    }


def _pack_python_sources(current, pack_root, manifest):
    files = set(pack_root.rglob('*.py'))
    omitted = set()
    packs_root = pack_root.parent
    for field in ('case_dirs', 'adapter_files', 'compatibility_files'):
        values = manifest.get(field, [])
        if not isinstance(values, list):
            omitted.add(f'manifest:{field}')
            continue
        for relative in values:
            if (not isinstance(relative, str) or '\\' in relative
                    or any(part in {'', '.', '..'} or part.startswith('.') or part in PRIVATE_PARTS - {'adapters'}
                           for part in relative.split('/'))):
                omitted.add(f'manifest:{field}')
                continue
            path = current / relative
            if (path.resolve() != path
                    or path.is_relative_to(packs_root) and not path.is_relative_to(pack_root)):
                omitted.add(f'manifest:{field}')
            elif field == 'case_dirs' and path.is_dir():
                files.update(path.rglob('*.py'))
            elif field != 'case_dirs' and path.is_file():
                if path.suffix == '.py':
                    files.add(path)
            else:
                omitted.add(f'manifest:{field}')
    return sorted(files), sorted(omitted)


def _static_pack_review(current, pack_root, manifest, entries):
    """Find explicit imports of platform modules whose contents differ from the target."""
    changed_modules = {
        Path(row['file']).stem
        for row in entries if row['currentSha256'] != row['targetSha256']
        and Path(row['file']).suffix == '.py'
        and (len(Path(row['file']).parts) == 1 or row['file'].split('/')[0] in {'test-platform', 'scripts'})
    }
    if not changed_modules:
        return [], []
    files, unparsed = _pack_python_sources(current, pack_root, manifest)
    sites = []
    for file in files:
        if any(part.startswith('.') or part in PRIVATE_PARTS - {'adapters'}
               for part in file.relative_to(current).parts):
            continue
        relative = file.relative_to(current).as_posix()
        try:
            path = declared_file(current, relative)
            if path is None:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeError, OSError, ValueError):
            unparsed.append(relative)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [(node.module, [entry.name for entry in node.names])]
            elif isinstance(node, ast.Import):
                modules = [(entry.name, []) for entry in node.names]
            else:
                continue
            for name, names in modules:
                if name.split('.')[-1] in changed_modules:
                    sites.append({'file': relative, 'line': node.lineno, 'module': name, 'names': names})
    return sorted(sites, key=lambda row: (row['file'], row['line'], row['module'])), unparsed


def _check_compatibility(manifest, api):
    spec = manifest.get('platform_api')
    features = spec.get('features') if isinstance(spec, dict) else None
    available = set(api['features'])
    missing = (sorted(set(features) - available)
               if isinstance(features, list) and all(isinstance(value, str) for value in features) else [])
    outcome = {'targetApiVersion': api['version'], 'targetFeatures': api['features'],
               'explicit': spec is not None, 'missingFeatures': missing}
    try:
        compatibility(manifest, api_version=api['version'], available_features=available)
    except ValueError as exc:
        return {**outcome, 'compatible': False, 'reason': str(exc)}
    return {**outcome, 'compatible': True}


def build_report(current, target, pack_id, *, baseline=None):
    current, target = Path(current).resolve(), Path(target).resolve()
    if not isinstance(pack_id, str) or pack_id in {'.', '..'} or not re.fullmatch(r'[A-Za-z0-9_.-]+', pack_id):
        raise ValueError('请选择合法的 Pack ID')
    pack_path = declared_file(current, '/'.join(('packs', pack_id, 'pack.json')))
    if pack_path is None:
        raise ValueError('当前副本缺少所选 Pack manifest')
    manifest = read_json(pack_path)
    if not isinstance(manifest, dict) or manifest.get('id', pack_id) != pack_id:
        raise ValueError('当前 Pack manifest ID 与所选目录不一致')

    target_files, target_api = verify_release(target)
    if baseline is None:
        baseline = declared_file(current, RECEIPT)
    else:
        baseline = Path(baseline)
        if baseline.is_dir():
            baseline = baseline / RECEIPT
        if not baseline.exists():
            baseline = None
    baseline_known = baseline is not None
    base_files = read_receipt(baseline)[0] if baseline_known else {}
    entries, counts = [], Counter()
    known_paths = sorted(set(base_files) | set(target_files) | _registered_paths(current))
    for relative in known_paths:
        path = declared_file(current, relative)
        local = sha256_file(path) if path is not None else None
        row = _compare(relative, base_files.get(relative), local, target_files.get(relative), baseline_known)
        counts[row['classification']] += 1
        if row['classification'] not in {'unchanged', 'matches_target'}:
            entries.append(row)

    protocol = _check_compatibility(manifest, target_api)
    review, unparsed = _static_pack_review(current, pack_path.parent, manifest, entries)
    if not protocol['compatible']:
        ok, status = False, 'failed'
    elif not baseline_known or counts['conflict'] or review or unparsed:
        ok, status = None, 'incomplete'
    else:
        ok, status = True, 'passed'
    return {
        'ok': ok,
        'status': status,
        'pack': pack_id,
        'baselineStatus': 'available' if baseline_known else 'unverified_origin',
        'summary': dict(sorted(counts.items())),
        'files': entries,
        'compatibility': protocol,
        'importSitesToReview': review,
        'unparsedPackPython': unparsed,
        'scope': '声明的平台文件、所选 Pack 的协议与静态导入；无运行时验收',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current', type=Path, default=ROOT, help='当前独立平台根目录')
    parser.add_argument('--target', type=Path, required=True, help='目标原始发行的 platform 目录')
    parser.add_argument('--baseline', type=Path, help='原始发行收据文件或包含收据的 platform 目录')
    parser.add_argument('--pack', required=True)
    args = parser.parse_args()
    try:
        report = build_report(args.current, args.target, args.pack, baseline=args.baseline)
    except (ValueError, OSError, RuntimeError, SyntaxError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {'passed': 0, 'failed': 1, 'incomplete': 2}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
