"""Read-only release comparison for detached synthetic project installations."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from project_upgrade import build_report


def write_files(root, files):
    for relative, value in files.items():
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value, encoding='utf-8')


def receipt(root, files, *, api=None):
    write_files(root, files)
    digests = {file: hashlib.sha256(value.encode()).hexdigest() for file, value in files.items()}
    value = {'kind': 'platform', 'files': digests, 'credentialsIncluded': False, 'runtimeDataIncluded': False}
    if api is not None:
        value['platformApi'] = api
    (root / 'distribution.json').write_text(json.dumps(value))


class ProjectUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current, self.target, self.original = (self.root / name for name in ('current', 'target', 'original'))
        protocol = "API_VERSION = '1.1'\nFEATURES = frozenset({'ready'})\n"
        new_protocol = "API_VERSION = '1.4'\nFEATURES = frozenset({'ready'})\nraise RuntimeError('must not import')\n"
        baseline = {'README.md': 'original shared readme', 'start.sh': 'start first', 'stop.sh': 'stop first',
                    'assets/style.css': 'same original', 'test-platform/fake_service.py': 'legacy = True\n',
                    'platform_protocol.py': protocol}
        receipt(self.original, baseline)
        target = {'README.md': 'upstream shared readme', 'start.sh': 'start first', 'requirements.txt': 'new',
                  'assets/style.css': 'same change', 'test-platform/fake_service.py': 'modern = True\n',
                  'platform_protocol.py': new_protocol}
        receipt(self.target, target)
        local = {**baseline, 'start.sh': 'local start', 'assets/style.css': 'same change',
                 'test-platform/fake_service.py': 'local = True\n', 'scripts/custom_fixture.py': 'pass\n'}
        manifest = {'id': 'sample', 'platform_api': {'min': '1.0', 'max_exclusive': '2.0',
                                                     'features': ['portable_receipt']}}
        write_files(self.current, {
            **local,
            'packs/sample/pack.json': json.dumps(manifest),
            'packs/sample/fixture.py': 'from fake_service import local\nraise RuntimeError("must not import")\n',
            'packs/other/do_not_scan.py': 'from fake_service import local\n',
            'resources/platform-core-files.json': json.dumps(['scripts/custom_fixture.py']),
            '.env.local': 'PRIVATE=synthetic-test-value',
            'data/observation.log': 'synthetic record',
        })

    def test_three_way_change_types_target_features_and_static_callsite(self):
        before = {file: file.read_bytes() for file in self.current.rglob('*') if file.is_file()}
        result = build_report(self.current, self.target, 'sample', baseline=self.original)
        rows = {row['file']: row for row in result['files']}
        self.assertEqual(rows['README.md']['classification'], 'upstream_only')
        self.assertEqual(rows['start.sh']['classification'], 'local_only')
        self.assertEqual(rows['stop.sh']['targetChange'], 'removed')
        self.assertEqual(rows['requirements.txt']['targetChange'], 'added')
        self.assertEqual(rows['assets/style.css']['classification'], 'same_change')
        self.assertEqual(rows['scripts/custom_fixture.py']['localChange'], 'added')
        self.assertEqual(rows['test-platform/fake_service.py']['classification'], 'conflict')
        self.assertEqual(result['compatibility']['targetApiVersion'], '1.4')
        self.assertEqual(result['compatibility']['missingFeatures'], ['portable_receipt'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['importSitesToReview'], [
            {'file': 'packs/sample/fixture.py', 'line': 1, 'module': 'fake_service', 'names': ['local']},
        ])
        self.assertEqual(before, {file: file.read_bytes() for file in self.current.rglob('*') if file.is_file()})
        self.assertNotIn('synthetic-test-value', json.dumps(result))
        self.assertFalse((self.current / '.git').exists())

    def test_missing_baseline_preserves_unknown_origin_and_known_compatibility(self):
        manifest = {'id': 'sample', 'platform_api': {'min': '1.0', 'max_exclusive': '2.0', 'features': ['ready']}}
        write_files(self.current, {'packs/sample/pack.json': json.dumps(manifest)})
        process = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts/project_upgrade.py'), '--current', str(self.current),
             '--target', str(self.target), '--pack', 'sample'], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(process.returncode, 2, process.stderr)
        result = json.loads(process.stdout)
        self.assertIsNone(result['ok'])
        self.assertEqual(result['baselineStatus'], 'unverified_origin')
        self.assertTrue(result['compatibility']['compatible'])
        self.assertTrue(all(row['classification'] == 'unknown_origin' for row in result['files']))

    def test_declared_cases_and_adapter_imports_outside_pack_directory_are_reviewed(self):
        manifest = {'id': 'sample', 'case_dirs': ['examples/sample_cases'],
                    'adapter_files': ['adapters/sample/client.py']}
        write_files(self.current, {
            'packs/sample/pack.json': json.dumps(manifest),
            'examples/sample_cases/read_cases.py': 'import fake_service\n',
            'adapters/sample/client.py': 'from fake_service import local\n',
        })
        result = build_report(self.current, self.target, 'sample', baseline=self.original)
        self.assertEqual([site['file'] for site in result['importSitesToReview']],
                         ['adapters/sample/client.py', 'examples/sample_cases/read_cases.py', 'packs/sample/fixture.py'])
        self.assertEqual(result['unparsedPackPython'], [])

    def test_target_requires_consistency_between_advertised_capabilities_and_content(self):
        target_receipt = self.target / 'distribution.json'
        changed = json.loads(target_receipt.read_text())
        changed['platformApi'] = {'version': '1.4', 'features': ['ready', 'portable_receipt']}
        target_receipt.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, '与协议文件不一致'):
            build_report(self.current, self.target, 'sample', baseline=self.original)

    def test_target_mismatch_and_nonneutral_paths_are_rejected(self):
        (self.target / 'README.md').write_text('bytes differ after export')
        with self.assertRaisesRegex(ValueError, '摘要不匹配'):
            build_report(self.current, self.target, 'sample', baseline=self.original)
        target_receipt = self.target / 'distribution.json'
        changed = json.loads(target_receipt.read_text())
        changed['files']['packs/other/pack.json'] = '1' * 64
        target_receipt.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, '项目资产'):
            build_report(self.current, self.target, 'sample', baseline=self.original)
