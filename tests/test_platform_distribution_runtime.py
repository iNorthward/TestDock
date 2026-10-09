"""Neutral platform acceptance; also ships in the standalone distribution."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PlatformDistributionRuntimeTests(unittest.TestCase):
    def probe(self, code):
        prefix = '''import sys, socket, os
from pathlib import Path
root = Path.cwd()
sys.path[:0] = [str(root), str(root / 'scripts'), str(root / 'test-platform'), str(root / 'test-platform/common')]
def forbidden(*args, **kwargs): raise AssertionError('network forbidden')
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = forbidden
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-S', '-c', prefix + code], cwd=ROOT,
                                    env={'PATH': os.environ['PATH'], 'PLATFORM_ENV_FILE': '/dev/null',
                                         'PLATFORM_PACKS': 'demo_pack', 'PYTHONDONTWRITEBYTECODE': '1',
                                         'PLATFORM_PACK_DATA_DIR': directory},
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_catalog_transport_history_and_tasks(self):
        self.probe('''import server, execution_jobs as jobs
result = server.cat.exec_case('DEMO-R01')
assert result['status'] == 'passed', result
identifier = server.rh.record_case(result, source='distribution')
assert server.rh.run_detail(identifier)['status'] == 'passed'
calls = []
operation = lambda: calls.append(1) or {'status': 'passed'}
first = jobs.run_idempotent(kind='distribution', request_id='example', payload={}, operation=operation)
second = jobs.run_idempotent(kind='distribution', request_id='example', payload={}, operation=operation)
assert len(calls) == 1
assert not any(name.startswith('packs.' + 'sample_project') for name in sys.modules)
''')

    def test_wire_rules_and_tools_have_no_business_fallback(self):
        self.probe('''from pack_registry import load_packs, load_pack_tool_module
load_packs()
import json_wire_types as wire
assert not hasattr(wire, 'example_framework' + '_int64')
wire.register_wire_rule('example_rule', lambda value: value if type(value) is int else None)
try: wire.register_wire_rule('json_integer', lambda value: value)
except ValueError: pass
else: raise AssertionError('builtin overridden')
try: load_pack_tool_module('sync_json_wire_types')
except ValueError as exc: assert '[incomplete]' in str(exc)
else: raise AssertionError('foreign tool loaded')
''')

    def test_contract_schema_and_profile_tools_accept_neutral_project(self):
        self.probe('''from pack_registry import load_packs
load_packs()
import apifox_contract_audit as audit, gen_interface_profile as profile
import sync_db_schema as schema, gen_r_series_candidates as candidates
old = {'paths': {'/v1/old': {'get': {}}}}
new = {'paths': {'/v1/new': {'get': {}}}}
assert audit.audit(old, new, set())['new_in_apifox'] == ['GET /v1/new']
assert '待确认' in profile.render('GET', '/v1/new', {}, {})
assert schema.compare_schemas({'tables': []}, {'tables': []})['new_tables'] == []
spec = {'responses': {'200': {'content': {'application/json': {'schema': {'type': 'array', 'items': {'type': 'string'}}}}}}}
assert candidates.derive_for_op('GET', '/v1/new', spec, {}) is not None
''')

    def test_asset_route_blocks_symlink_escape(self):
        self.probe('''import io, tempfile
from server import H
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    asset = root / 'assets'; asset.mkdir()
    secret = root / 'outside.txt'; secret.write_text('outside')
    (asset / 'escape.txt').symlink_to(secret)
    handler = object.__new__(H)
    codes = []
    handler.send_error = codes.append
    handler._serve_repo_asset('escape.txt', asset_root=asset)
    assert codes == [403], codes
''')
