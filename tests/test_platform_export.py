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
