from __future__ import annotations

import os
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from platform_config import panel_port
from create_project_pack import create
from export_platform import build


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

    def test_generated_launcher_default_and_explicit_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(ROOT / 'examples', root / 'examples', ignore=shutil.ignore_patterns('__pycache__'))
            launch = create('sample_launch', 'Sample', 18898, root=root) / 'launch.sh'
            delegate = root / 'start.sh'
            delegate.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$PLATFORM_EXPECTED_PACK" "$PLATFORM_ENV_FILE" "$1"\n')
            delegate.chmod(0o755)
            environment = {'PATH': os.environ['PATH']}
            for setup, expected in [('unset PLATFORM_ENV_FILE', '.env.sample_launch'),
                                    ('export PLATFORM_ENV_FILE=.env.sample_review', '.env.sample_review')]:
                for command in ('start', 'status', 'stop'):
                    with self.subTest(setup=setup, command=command):
                        result = subprocess.run(
                            ['bash', '-c', setup + '\nexec "$@"', 'launcher-test', str(launch), command],
                            cwd=td, env=environment, check=True, capture_output=True, text=True, timeout=15,
                        )
                        self.assertEqual(result.stdout.splitlines(), ['sample_launch', expected, command])

    @unittest.skipIf(os.environ.get('PLATFORM_OFFLINE_CONTEXT'), '单独运行自有回环启动验收')
    def test_two_generated_launchers_stop_only_their_own_panel(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            root = build(temp / 'release') / 'platform'
            with socket.socket() as first, socket.socket() as second:
                for probe in (first, second):
                    probe.bind(('127.0.0.1', 0))
                ports = [probe.getsockname()[1] for probe in (first, second)]
            for pack, port in zip(('sample_launch_a', 'sample_launch_b'), ports):
                create(pack, pack, port, root=root)

            environment = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1'}

            def launch(pack, command, *, selected=None, environment_pack=None):
                env = dict(environment)
                if selected is not None:
                    env['PLATFORM_ENV_FILE'] = selected
                if environment_pack is not None:
                    env['PLATFORM_PACKS'] = environment_pack
                return subprocess.run([str(root / 'packs' / pack / 'launch.sh'), command], cwd=temp, env=env,
                                      capture_output=True, text=True, timeout=30)

            def get_platform(port):
                with subprocess.Popen(['curl', '-fsS', '--noproxy', '*', '--max-time', '5',
                                       f'http://127.0.0.1:{port}/api/session'],
                                      env=environment, stdout=subprocess.PIPE, text=True) as process:
                    token = json.load(process.stdout)['token']
                output = subprocess.check_output(
                    ['curl', '-fsS', '--noproxy', '*', '--max-time', '5', '-H', 'X-TestDock-Token: ' + token,
                     f'http://127.0.0.1:{port}/api/platform'], env=environment, text=True,
                )
                return json.loads(output)['data']['pack']

            try:
                for pack, port in zip(('sample_launch_a', 'sample_launch_b'), ports):
                    started = launch(pack, 'start')
                    self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                    self.assertEqual(get_platform(port), pack)
                    self.assertIn('运行中', launch(pack, 'status').stdout)
                for environment_pack in (None, 'sample_launch_a'):
                    rejected = launch('sample_launch_a', 'stop', selected='.env.sample_launch_b',
                                      environment_pack=environment_pack)
                    self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
                self.assertEqual(launch('sample_launch_a', 'stop').returncode, 0)
                self.assertIn('未运行', launch('sample_launch_a', 'status').stdout)
                self.assertEqual(get_platform(ports[1]), 'sample_launch_b')
                self.assertIn('运行中', launch('sample_launch_b', 'status', selected='.env.sample_launch_b').stdout)
            finally:
                cleanup = [launch(pack, 'stop') for pack in ('sample_launch_a', 'sample_launch_b')]
                for stopped in cleanup:
                    self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)


if __name__ == '__main__':
    unittest.main()
