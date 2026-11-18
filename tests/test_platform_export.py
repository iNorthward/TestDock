"""Physical distribution split and reinstallation in isolated copies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from export_platform import build, _tree_files
from create_project_pack import create
from platform_release import verify_release
from verify_project_pack import verify


class PlatformExportTests(unittest.TestCase):
    def test_existing_output_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                build(Path(directory))

    def test_export_excludes_credentials_and_mutable_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack = root / 'packs/sample'; pack.mkdir(parents=True)
            for name in ['identity_pool.json', 'client_pool.json', '.env.local', 'history.db',
                         'identity_pool.example.json', 'pack.json']:
                (pack / name).write_text('synthetic fixture')
            self.assertEqual(list(_tree_files(root, 'packs/sample')),
                             ['packs/sample/identity_pool.example.json', 'packs/sample/pack.json'])

    def test_export_without_an_installed_demo_remains_runnable(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            installed = build(temp / 'installed') / 'platform'
            shutil.rmtree(installed / 'packs/demo_pack')
            rebuilt = build(temp / 'rebuilt', source=installed) / 'platform'
            verified, api = verify_release(rebuilt)
            self.assertIn('packs/demo_pack/pack.json', verified)
            self.assertIn('platform_protocol.py', verified)
            self.assertIn('workbench_components', api['features'])
            self.assertEqual(verify('demo_pack', root=rebuilt, smoke='DEMO-R01')['mockPassed'], 'DEMO-R01')

    def test_two_project_variants_export_only_the_selected_local_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original = build(temp / 'original') / 'platform'
            expected, _ = verify_release(original)
            derived = shutil.copytree(original, temp / 'project')
            create('sample_alpha', 'Sample Alpha', 18812, root=derived)
            create('sample_beta', 'Sample Beta', 18813, root=derived)
            custom_files = ('README.md', 'AGENTS.md', 'start.sh', 'assets/platform-panel.css',
                            'test-platform/insights.html')
            for pack_id, mark in [('sample_alpha', 'CustomVariantAlpha'), ('sample_beta', 'CustomVariantBeta')]:
                with self.subTest(pack=pack_id):
                    for relative in custom_files:
                        (derived / relative).write_text(mark)
                    pack_readme = derived / 'packs' / pack_id / 'README.md'
                    pack_readme.write_text(mark)
                    with self.assertRaisesRegex(ValueError, '摘要不匹配'):
                        build(temp / ('rejected-' + pack_id), source=derived)
                    output = build(temp / ('result-' + pack_id), source=derived,
                                   pack_ids=[pack_id], neutral_source=original)
                    reexported, _ = verify_release(output / 'platform')
                    self.assertEqual(expected, reexported)
                    self.assertEqual({path.name for path in output.iterdir()}, {'platform', 'pack-' + pack_id})
                    self.assertEqual((output / ('pack-' + pack_id) / 'packs' / pack_id / 'README.md').read_text(), mark)
                    self.assertFalse(list((output / ('pack-' + pack_id)).glob('.env.*')))

    def test_missing_or_changed_neutral_input_cannot_publish_output(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original = build(temp / 'original') / 'platform'
            (original / 'assets/platform-panel.css').unlink()
            with self.assertRaisesRegex(ValueError, '原始发行文件缺失'):
                build(temp / 'missing', neutral_source=original)
            self.assertFalse((temp / 'missing').exists())
            (original / 'assets/platform-panel.css').write_text('different CSS')
            with self.assertRaisesRegex(ValueError, '原始发行文件摘要不匹配'):
                build(temp / 'different', neutral_source=original)
            self.assertFalse((temp / 'different').exists())
