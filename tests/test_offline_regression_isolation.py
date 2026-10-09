"""Synthetic child processes; fixtures never point at actual credentials/data."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import targeted_regression as gate


class OfflineRegressionIsolationTests(unittest.TestCase):
    def run_node(self, code, root, **environment):
        probe = root / 'test_probe.js'
        probe.write_text(code)
        plan = {'groups': [], 'python': [], 'javascript': [str(probe)],
                'unmappedSources': [], 'sourceMapping': {}, 'businessCasesExecuted': 0}
        with patch.object(gate, 'ROOT', root), patch.dict(os.environ, environment):
            return gate.execute_plan(plan)

    def test_node_does_not_inherit_parent_credentials_or_environment_selector(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_node("const assert=require('node:assert/strict');"
                                   "assert.equal(process.env.EXAMPLE_PROJECT_SYNTHETIC_PARENT_SECRET,undefined);"
                                   "assert.equal(process.env.PLATFORM_ENV_FILE,'/dev/null');", root,
                                   EXAMPLE_PROJECT_SYNTHETIC_PARENT_SECRET='synthetic-parent', PLATFORM_ENV_FILE=str(root / 'parent.env'))
        self.assertTrue(result['ok'])

    def test_node_uses_temporary_data_until_process_finishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data, artifacts = root / 'parent-data', root / 'parent-artifacts'
            data.mkdir()
            artifacts.mkdir()
            result = self.run_node("const fs=require('node:fs');const path=require('node:path');"
                                   "fs.writeFileSync(path.join(process.env.PLATFORM_PACK_DATA_DIR,'probe'),'synthetic');"
                                   "fs.writeFileSync(path.join(process.env.PLATFORM_ARTIFACT_ROOT,'probe'),'synthetic');", root,
                                   PLATFORM_PACK_DATA_DIR=str(data), PLATFORM_ARTIFACT_ROOT=str(artifacts))
            self.assertTrue(result['ok'])
            self.assertFalse((data / 'probe').exists())
            self.assertFalse((artifacts / 'probe').exists())
            self.assertTrue(result['offlineIsolation']['temporaryDataRemoved'])

    def test_caught_node_network_attempt_still_fails_gate(self):
        with TemporaryDirectory() as directory:
            result = self.run_node("const assert=require('node:assert/strict');"
                                   "assert.throws(()=>require('node:net').connect({host:'127.0.0.1',port:9}),/离线/);",
                                   Path(directory))
        self.assertFalse(result['ok'])
        self.assertEqual(result['nodeExitCode'], 0)
        self.assertEqual(result['blockedConnectionAttempts'], 1)

    def test_node_child_with_empty_env_keeps_network_and_data_guards(self):
        import json
        with TemporaryDirectory() as directory:
            root = Path(directory)
            child = "const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');" \
                    "assert.equal(process.env.PLATFORM_ENV_FILE,'/dev/null');" \
                    "fs.writeFileSync(path.join(process.env.PLATFORM_PACK_DATA_DIR,'child'),'synthetic');" \
                    "assert.throws(()=>require('node:net').connect({host:'127.0.0.1',port:9}),/离线/);"
            code = "const assert=require('node:assert/strict');const cp=require('node:child_process');" \
                   "const child=cp.spawnSync(process.execPath,['-e'," + json.dumps(child) + "],{env:{},encoding:'utf8'});" \
                   "assert.equal(child.status,0,child.stderr);"
            result = self.run_node(code, root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['blockedConnectionAttempts'], 1)
        self.assertEqual(result['nodeExitCode'], 0)

    def test_python_spawned_by_node_keeps_guard_with_empty_env(self):
        import json
        with TemporaryDirectory() as directory:
            child = "import os,socket; assert os.environ['PLATFORM_ENV_FILE']=='/dev/null'\n" \
                    "try: socket.socket().connect(('127.0.0.1',9))\n" \
                    "except AssertionError: pass\n" \
                    "else: raise AssertionError('missing offline guard')\n"
            code = "const assert=require('node:assert/strict');const cp=require('node:child_process');" \
                   "const child=cp.spawnSync(" + json.dumps(sys.executable) + ",['-c'," + json.dumps(child) + "],{env:{},encoding:'utf8'});" \
                   "assert.equal(child.status,0,child.stderr);"
            result = self.run_node(code, Path(directory))
        self.assertFalse(result['ok'])
        self.assertEqual(result['blockedConnectionAttempts'], 1)
        self.assertEqual(result['nodeExitCode'], 0)

    def test_node_cannot_read_parent_env_or_write_protected_source_fixture(self):
        import json
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env_file, source = root / 'parent.env', root / 'source.txt'
            env_file.write_text('SYNTHETIC=synthetic-offline-value')
            source.write_text('original')
            code = "const assert=require('node:assert/strict');const fs=require('node:fs');" \
                   "assert.throws(()=>fs.readFileSync(" + json.dumps(str(env_file)) + "),/离线/);" \
                   "assert.throws(()=>fs.writeFileSync(" + json.dumps(str(source)) + ",'changed'),/离线/);"
            result = self.run_node(code, root, PLATFORM_ENV_FILE=str(env_file))
            self.assertEqual(source.read_text(), 'original')
        self.assertFalse(result['ok'])
        self.assertEqual(result['blockedFilesystemAttempts'], 2)
        self.assertEqual(result['nodeExitCode'], 0)

    def test_fresh_python_worker_and_descendant_do_not_inherit_parent_secret(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/test_probe.py').write_text(
                "import os, subprocess, sys, unittest\n"
                "class Probe(unittest.TestCase):\n"
                " def test_env(self):\n"
                "  self.assertNotIn('EXAMPLE_PROJECT_SYNTHETIC_PARENT_SECRET',os.environ)\n"
                "  self.assertEqual(os.environ['PLATFORM_ENV_FILE'],'/dev/null')\n"
                "  child=subprocess.run([sys.executable,'-c',\"import os; assert 'EXAMPLE_PROJECT_SYNTHETIC_PARENT_SECRET' not in os.environ; assert os.environ['PLATFORM_ENV_FILE']=='/dev/null'\"],capture_output=True,text=True)\n"
                "  self.assertEqual(child.returncode,0,child.stderr)\n")
            plan = {'groups': [], 'python': ['test_probe'], 'javascript': [],
                    'unmappedSources': [], 'sourceMapping': {}, 'businessCasesExecuted': 0}
            with patch.object(gate, 'ROOT', root), patch.dict(os.environ, EXAMPLE_PROJECT_SYNTHETIC_PARENT_SECRET='synthetic-parent'):
                result = gate.execute_plan(plan)
        self.assertTrue(result['ok'])
        self.assertEqual(result['testsRun'], 1)

    def test_unavailable_native_backend_cannot_be_reported_as_passing(self):
        with TemporaryDirectory() as directory, patch('offline_process.system_command', side_effect=ValueError('[incomplete] synthetic unavailable')):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                self.run_node('throw new Error("should not execute")', Path(directory))


if __name__ == '__main__':
    unittest.main()
