from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from platform_config import panel_port


class PlatformLaunchIsolationTest(unittest.TestCase):
    def test_launcher_resolves_demo_port_without_installing_business_adapter(self):
        code = 'from platform_launch_config import launch_configuration; import sys; '
        code += 'assert launch_configuration() == ("demo_pack", 18899); '
        code += 'assert "api_client" not in sys.modules and "catalog" not in sys.modules'
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT / 'scripts'),
                                     'PLATFORM_ENV_FILE': '/dev/null', 'PLATFORM_PACKS': 'demo_pack', 'PLATFORM_PANEL_PORT': '18899'},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_ports_are_rejected(self):
        for value in ('0', '65536', '-1', '12.5', 'True', '１２３', 'bad'):
            with self.subTest(value=value), patch.dict(os.environ, {'PLATFORM_PANEL_PORT': value}), \
                    self.assertRaisesRegex(ValueError, 'PLATFORM_PANEL_PORT'):
                panel_port()
        with patch.dict(os.environ, {'PLATFORM_PANEL_PORT': ''}):
            self.assertEqual(panel_port(), 8801)

    def test_process_ownership_requires_the_same_project_and_port(self):
        script = (ROOT / 'start.sh').read_text()
        start = script.index('is_our_process() {')
        function = script[start:script.index('# double-fork', start)]
        with tempfile.TemporaryDirectory() as td:
            code = 'ROOT="$FIXTURE_ROOT"; RUN_DIR="$FIXTURE_ROOT/run"; PACK_ID=demo_pack; TEST_PORT=18899\n'
            code += 'process_command() { printf "%s" "$FIXTURE_COMMAND"; }\n' + function
            code += 'if is_our_process 123 test-platform/server.py; then printf yes; else printf no; fi'
            for suffix, expected in (('--pack=demo_pack --port=18899', 'yes'),
                                     ('--pack=sample_project --port=18899', 'no'),
                                     ('--pack=demo_pack --port=8799', 'no'), ('', 'no')):
                with self.subTest(suffix=suffix):
                    result = subprocess.run(['bash', '-c', code], env={
                        'PATH': os.environ['PATH'], 'FIXTURE_ROOT': td,
                        'FIXTURE_COMMAND': f'python3 {td}/test-platform/server.py {suffix}',
                    }, capture_output=True, text=True, timeout=10, check=True)
                    self.assertEqual(result.stdout, expected)


if __name__ == '__main__':
    unittest.main()
