#!/usr/bin/env python3
"""Build an independent platform and separately installable selected Pack bundles."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def _copy(source, destination, relative):
    origin = (source / relative).resolve()
    if not origin.is_relative_to(source.resolve()) or not origin.is_file():
        raise ValueError(f"发行文件不存在或越界: {relative}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, target)
    target.chmod(origin.stat().st_mode & 0o777)


def _tree_files(source, relative):
    folder = source / relative
    forbidden = {'__pycache__', '.git', '.run', '.execution-locks', 'node_modules'}
    for path in sorted(folder.rglob('*')):
        if any(part in forbidden for part in path.relative_to(folder).parts):
            continue
        if path.suffix in {'.db', '.sqlite', '.sqlite3', '.pyc', '.log'} or path.name.endswith('_pool.json'):
            continue
        if path.name.startswith('.') and path.name != '.env.example':
            continue
        if not path.is_file():
            continue
        yield path.relative_to(source).as_posix()


def _receipt(folder, kind):
    hashes = {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(folder.rglob('*')) if path.is_file()}
    (folder / 'distribution.json').write_text(json.dumps(
        {'kind': kind, 'files': hashes, 'credentialsIncluded': False, 'runtimeDataIncluded': False},
        ensure_ascii=False, indent=2) + '\n')


def build(destination, *, source=ROOT, pack_ids=()):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination or source.is_relative_to(destination):
        raise ValueError('发行目标须为不存在的新目录，不能覆盖源码或已有发行目录')
    sys.path[:0] = [str(source), str(source / 'test-platform')]
    from pack_registry import _read_candidates, _validate_manifest
    inventory = json.loads((source / 'resources/platform-core-files.json').read_text())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='platform-dist-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        platform = staging / 'platform'
        platform.mkdir()
        support = ['test-platform/document.html', 'test-platform/panel.html', 'test-platform/insights.html', 'start.sh', 'stop.sh', 'scripts/load_env.sh', 'scripts/run_nightly.sh',
                   'scripts/offline_guard.cjs', 'resources/platform-core-files.json',
                   'requirements.txt', '.nvmrc', '.env.example', 'AGENTS.md', 'README.md', 'tests/test_platform_distribution_runtime.py']
        for relative in dict.fromkeys([*inventory, *support, *_tree_files(source, 'assets'), *_tree_files(source, 'docs/architecture'), *_tree_files(source, 'docs/onboarding'), *_tree_files(source, 'docs/reports/mature-foundation'), *_tree_files(source, 'docs/reports/six-improvements')]):
            _copy(source, platform, relative)
        # Platform examples are standalone projects, never default business policy.
        for relative in [*_tree_files(source, str(Path('packs') / 'demo_pack')), *_tree_files(source, str(Path('examples') / 'demo_pack')), *_tree_files(source, str(Path('examples') / 'http_pack_template')), *_tree_files(source, 'examples/adapter_recipes')]:
            _copy(source, platform, relative)
        manifest = platform / 'packs' / 'demo_pack' / 'pack.json'
        demo = json.loads(manifest.read_text()); demo['default'] = True
        manifest.write_text(json.dumps(demo, indent=2) + '\n')
        # Ship every declared generic test and its helpers, so installing a Pack
        # preserves the same source-to-test mapping as the development checkout.
        regression = json.loads((source / 'resources/targeted-regression.json').read_text())
        generic_tests = set()
        for group in regression['groups'].values():
            generic_tests.update('tests/' + name + '.py' for name in group.get('python', []))
            generic_tests.update(group.get('javascript', []))
        generic_tests.update(relative for relative in _tree_files(source, 'tests')
                             if not Path(relative).name.startswith('test_'))
        for relative in sorted(generic_tests):
            _copy(source, platform, relative)
        _copy(source, platform, 'resources/targeted-regression.json')
        (platform / '.gitignore').write_text(
            '__pycache__/\n*.py[cod]\n.venv/\n.env*\n!.env.example\n.run/\n'
            'data/\nartifacts/\npacks/*/data/identity_pool.json\n*.db\n*.sqlite*\n.DS_Store\n')
        _receipt(platform, 'platform')
        for pack_id in pack_ids:
            candidates = _read_candidates(pack_id)
            if len(candidates) != 1:
                raise ValueError(f'Pack 不存在或不唯一: {pack_id}')
            pack = _validate_manifest(candidates[0], candidates[0]['manifest_path'])
            bundle = staging / ('pack-' + pack_id)
            bundle.mkdir()
            folders = [pack['root'].relative_to(source).as_posix(), *pack.get('case_dirs', [])]
            files = {*pack.get('adapter_files', []), *pack.get('compatibility_files', [])}
            exclusions = pack.get('distribution_excludes', [])
            for folder in folders:
                for relative in _tree_files(source, folder):
                    origin = source / relative
                    local = origin.relative_to(pack['root']).as_posix() if origin.is_relative_to(pack['root']) else relative
                    if not any(fnmatch.fnmatchcase(local, pattern) for pattern in exclusions):
                        files.add(relative)
            # Tests and fixtures belong inside this Pack or its declared case directories.
            # Never copy the platform-wide tests tree into another project's bundle.
            for relative in sorted(files):
                if relative in inventory or relative.startswith(('resources/platform-', 'scripts/')):
                    raise ValueError(f'Pack 发行文件不得覆盖 Core: {relative}')
                _copy(source, bundle, relative)
            _receipt(bundle, 'pack-' + pack_id)
        # The temporary staging directory is renamed only after every bundle succeeds.
        staging.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pack', action='append', default=[])
    args = parser.parse_args()
    try:
        print(build(args.output, pack_ids=args.pack))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
