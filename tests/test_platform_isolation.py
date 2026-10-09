from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test-platform"))

import pack_registry as registry


class PackIsolationTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        (self.root / "packs/acme").mkdir(parents=True)
        (self.root / "cases").mkdir()
        self.manifest = self.root / "packs/acme/pack.json"
        self.pack = {"id": "acme", "case_dirs": ["cases"]}
        self.patches = [patch.object(registry, name, value) for name, value in (
            ("REPO_ROOT", self.root), ("PACKS_ROOT", self.root / "packs"), ("_PACKS", None),
            ("_CANDIDATES", None), ("_INITIALIZING", False), ("_BOOTSTRAP_FAILED", False), ("_PACK_SELECTOR", None),
        )]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        namespace = patch.dict(sys.modules, {'packs': None})
        namespace.start()
        self.addCleanup(namespace.stop)

    def test_foreign_extension_targets_are_rejected_before_any_import(self):
        (self.root / 'packs/other').mkdir()
        (self.root / 'packs/other/install.py').write_text('raise AssertionError("foreign code executed")')
        for field in ('bootstrap', 'catalog_extension', 'panel_extension'):
            with self.subTest(field=field), patch.object(registry.importlib, 'import_module') as importer:
                with self.assertRaisesRegex(RuntimeError, '其他 Pack'):
                    registry._validate_manifest({**self.pack, field: 'packs.other.install:install'}, self.manifest)
                importer.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, '其他 Pack'):
            registry._validate_manifest({**self.pack, 'panel_services': {'bad': 'packs.other.absent'}, 'optional_panel_services': ['bad']}, self.manifest)

    def test_foreign_source_symlink_cannot_become_an_owned_entry(self):
        (self.root / 'packs/other').mkdir()
        target = self.root / 'packs/other/install.py'
        target.write_text('def install(): pass')
        (self.manifest.parent / 'entry.py').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, '不属于当前项目'):
            registry._validate_manifest({**self.pack, 'bootstrap': 'packs.acme.entry:install'}, self.manifest)

    def test_parent_package_cannot_execute_foreign_initializer_before_preflight(self):
        (self.root / 'packs/__init__.py').write_text('raise AssertionError("initializer executed")')
        (self.manifest.parent / 'entry.py').write_text('def install(): pass')
        with patch.object(registry.importlib, 'import_module') as importer:
            with self.assertRaisesRegex(RuntimeError, '不属于当前项目'):
                registry._validate_manifest({**self.pack, 'bootstrap': 'packs.acme.entry:install'}, self.manifest)
            importer.assert_not_called()

    def test_core_services_require_explicit_shared_capability_declaration(self):
        (self.root / 'test-platform').mkdir()
        (self.root / 'test-platform/run_history.py').write_text('"""Shared core."""')
        manifest = {**self.pack, 'panel_services': {'history': 'run_history'}}
        with patch.dict(sys.modules, {'run_history': None}):
            with self.assertRaisesRegex(RuntimeError, '不属于当前项目'):
                registry._validate_manifest(manifest, self.manifest)
            validated = registry._validate_manifest({**manifest, 'shared_core_services': ['history']}, self.manifest)
            self.assertEqual(validated['shared_core_services'], ['history'])
            with self.assertRaisesRegex(RuntimeError, '不能声明 Core'):
                registry._validate_manifest({**self.pack, 'adapter_files': ['test-platform/run_history.py']}, self.manifest)

    def test_cached_module_cannot_override_verified_source(self):
        (self.manifest.parent / 'entry.py').write_text('def install(): pass')
        with patch.dict(sys.modules, {'packs.acme.entry': SimpleNamespace(__file__=str(self.root / 'foreign.py'))}), \
                patch.object(registry.importlib, 'import_module') as importer:
            with self.assertRaisesRegex(RuntimeError, '其他来源模块占用'):
                registry._validate_manifest({**self.pack, 'bootstrap': 'packs.acme.entry:install'}, self.manifest)
            importer.assert_not_called()

    def test_two_projects_are_rejected_before_any_bootstrap(self):
        installer = Mock()
        with patch.dict(os.environ, {"PLATFORM_PACKS": "acme,other", "PLATFORM_ENV_FILE": "/dev/null"}), \
                patch.object(registry, "_prepare_pack", installer):
            with self.assertRaisesRegex(RuntimeError, "只能选择一个"):
                registry.load_packs()
        installer.assert_not_called()

    def test_manifest_shapes_and_paths_fail_before_defaults_or_bootstrap(self):
        invalid = [[], {**self.pack, "case_dirs": "cases"}, {**self.pack, "config_defaults": []},
                   {**self.pack, "default": "false"}, {**self.pack, "navigation": [{}]},
                   {**self.pack, "case_dirs": ["../outside"]},
                   {**self.pack, "case_dirs": [str(self.root / "cases")]},
                   {**self.pack, "identity_pool_file": "../other/data/pool.json"},
                   {**self.pack, "bootstrap": "invalid"},
                   {**self.pack, "optional_environment_checks": [{'name': 'ops', 'variables': 'SECRET'}]},
                   {**self.pack, "identity_requirements": {'distinct_roles': [{}]}},
                   {**self.pack, "config_defaults": {"PLATFORM_PACKS": "other"}}]
        for pack in invalid:
            with self.subTest(pack=pack), self.assertRaises(RuntimeError):
                registry._validate_manifest(pack, self.manifest)

    def test_manifest_id_cannot_alias_another_project_directory(self):
        with self.assertRaisesRegex(RuntimeError, "目录名一致"):
            registry._validate_manifest({**self.pack, "id": "other"}, self.manifest)

    def test_duplicate_pack_ids_are_not_silently_overwritten(self):
        with patch.dict(os.environ, {"PLATFORM_PACKS": "acme"}), self.assertRaisesRegex(RuntimeError, "id 重复"):
            registry._select([{'id': 'acme'}, {'id': 'acme'}])

    def test_source_symlink_and_other_pack_resources_are_rejected(self):
        external = self.root.parent / "outside_cases.py"
        (self.root / "cases/escape_cases.py").symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, "仓库目录内"):
            registry._validate_manifest(self.pack, self.manifest)
        (self.root / "cases/escape_cases.py").unlink()
        (self.root / "packs/other/cases").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "其他 pack"):
            registry._validate_manifest({**self.pack, "case_dirs": ["packs/other/cases"]}, self.manifest)

    def test_duplicate_module_names_are_rejected_before_loading(self):
        (self.root / "cases/duplicate_cases.py").write_text("")
        (self.root / "other_cases").mkdir()
        (self.root / "other_cases/duplicate_cases.py").write_text("")
        with self.assertRaisesRegex(RuntimeError, "case module 重复"):
            registry._validate_manifest({**self.pack, "case_dirs": ["cases", "other_cases"]}, self.manifest)

    def test_hot_reload_cannot_follow_new_source_symlink_outside_project(self):
        source = self.root / 'cases/hot_cases.py'
        source.write_text('CATALOG=[]')
        pack = {'id': 'acme', 'root': (self.root / 'packs/acme').resolve(), 'case_dirs': ['cases']}
        self.assertEqual(len(registry.case_source_files([pack])), 1)
        source.unlink()
        source.symlink_to(self.root.parent / 'outside_cases.py')
        with self.assertRaisesRegex(RuntimeError, '仓库目录内'):
            registry.case_source_files([pack])

    def test_inactive_broken_manifest_does_not_break_explicit_project(self):
        self.manifest.write_text(json.dumps(self.pack))
        (self.root / "packs/other").mkdir()
        (self.root / "packs/other/pack.json").write_text("broken json")
        with patch.dict(os.environ, {"PLATFORM_PACKS": "acme", "PLATFORM_ENV_FILE": "/dev/null"}), \
                patch.object(registry, "_prepare_pack", side_effect=lambda pack: pack):
            self.assertEqual([pack['id'] for pack in registry.load_packs()], ['acme'])
            os.environ['PLATFORM_PACKS'] = 'other'
            with self.assertRaisesRegex(RuntimeError, "重启进程"):
                registry.load_packs()

    def test_failed_bootstrap_cannot_be_retried_with_partial_globals(self):
        self.manifest.write_text(json.dumps(self.pack))
        with patch.dict(os.environ, {"PLATFORM_PACKS": "acme", "PLATFORM_ENV_FILE": "/dev/null"}), \
                patch.object(registry, "_prepare_pack", side_effect=RuntimeError("installer failed")) as install:
            with self.assertRaisesRegex(RuntimeError, "installer failed"):
                registry.load_packs()
            with self.assertRaisesRegex(RuntimeError, "初始化曾失败"):
                registry.load_packs()
            self.assertEqual(install.call_count, 1)

    def test_optional_service_only_tolerates_absent_declared_module(self):
        module = 'packs.acme.pack_ops'
        pack = {**self.pack, 'root': self.manifest.parent, 'panel_services': {'ops': module}, 'optional_panel_services': ['ops']}
        self.assertEqual(registry.load_panel_services([pack]), {})
        with self.assertRaises(ModuleNotFoundError):
            registry.load_panel_services([{**pack, 'optional_panel_services': []}])
        (self.manifest.parent / 'pack_ops.py').write_text('import other_dependency')
        broken_dependency = ModuleNotFoundError("broken dependency", name="other_dependency")
        with patch.object(registry.importlib, "import_module", side_effect=broken_dependency):
            with self.assertRaises(ModuleNotFoundError):
                registry.load_panel_services([pack])


class RuntimeIsolationTest(unittest.TestCase):
    def test_core_does_not_infer_login_roles_from_project_route_names(self):
        catalog = importlib.import_module('catalog')
        module = SimpleNamespace(RUNNER_API={'probe': 'GET /agent/private'})
        with patch('identity_pool.get_identity') as lookup:
            catalog._ensure_case_identities({'expect': 'probe'}, module)
        lookup.assert_not_called()

    def test_failed_reload_blocks_execution_before_auth_or_runner(self):
        catalog = importlib.import_module('catalog')
        with patch.object(catalog, '_RELOAD_FAILED', False), \
                patch.object(catalog, '_load_case_modules', side_effect=RuntimeError('invalid source')), \
                patch.object(catalog, '_call_catalog_extensions') as hooks:
            with self.assertRaisesRegex(RuntimeError, 'invalid source'):
                catalog.reload_registry()
            with self.assertRaisesRegex(RuntimeError, '已停止案例执行'):
                catalog.exec_case('any')
            # The guard is reached before selection; no batch cases execute.
            with self.assertRaisesRegex(RuntimeError, '已停止案例执行'):
                catalog.run_all('safe', 'empty-fixture-suite')
            hooks.assert_not_called()
    def test_boundary_checker_catches_project_dependencies_and_paths(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        checker = importlib.import_module('check_platform_isolation')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'core.py').write_text('"""packs/sample_project/knowledge documentation only."""\n'
                                          'from adapters import sample_project\nimport foreign_cases\n'
                                          'PATH = "packs/sample_project/knowledge/reports"\n')
            findings = checker.core_findings(root, core_files=['core.py'], business_modules={'foreign_cases'})
            self.assertEqual([item['kind'] for item in findings], ['business_import', 'business_import', 'business_path'])

    def test_generic_identity_audit_does_not_infer_sample_project_roles_or_scenarios(self):
        audit = importlib.import_module('identity_pool_audit_core')
        role = audit.describe_role('agent', 'agent_parent', {})
        scene = audit.describe_pool_entry('order.default', 'scenario', {})
        self.assertEqual(role['purpose'], '使用位置待配置')
        self.assertEqual(role['needs']['fields'], [])
        self.assertNotIn('ATF-', str(role))
        self.assertEqual(scene['suite'], '')
        self.assertNotIn('订单交易', str(scene))

    def test_boundary_checker_rejects_unregistered_local_core_dependency(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        checker = importlib.import_module('check_platform_isolation')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'scripts').mkdir()
            (root / 'core.py').write_text('import hidden_tool\n')
            (root / 'scripts/hidden_tool.py').write_text('import packs.foreign\n')
            findings = checker.core_findings(root, core_files=['core.py'])
            self.assertEqual([item['kind'] for item in findings], ['unowned_core_dependency'])
            findings = checker.core_findings(root, core_files=['core.py', 'scripts/hidden_tool.py'])
            self.assertEqual([item['kind'] for item in findings], ['business_import'])

    def test_identity_relationships_are_enforced_only_when_declared_by_pack(self):
        audit = importlib.import_module('identity_pool_audit_core')
        pool = {'identities': [
            {'id': 'one', 'category': 'agent', 'role': 'primary', 'uid': 'same'},
            {'id': 'two', 'category': 'agent', 'role': 'secondary', 'uid': 'same'},
        ]}
        pack = {'identity_requirements': {'agent': ['primary', 'secondary']}}
        kwargs = dict(pack_id='demo', pool=pool, code_scan={}, pool_file='temporary')
        self.assertEqual(audit.build_audit(pack=pack, **kwargs)['missing_roles'], [])
        pack['identity_requirements']['distinct_roles'] = [{
            'category': 'agent', 'role': 'secondary', 'other_role': 'primary', 'fields': ['uid'], 'unique': True,
        }]
        report = audit.build_audit(pack=pack, **kwargs)
        self.assertEqual([role['role'] for role in report['missing_roles']], ['secondary'])

    def test_demo_import_and_single_mock_case_never_load_sample_project_or_open_network(self):
        code = '''
import socket, sys
sys.path.insert(0, "test-platform")
def forbidden(*args, **kwargs):
    raise AssertionError("business socket opened")
socket.socket.connect = forbidden
import server, identity_pool
from project_paths import runtime_data_path
assert server.rh.DB_PATH == runtime_data_path("run_history.db")
assert [c["id"] for c in server.cat._ALL] == ["DEMO-R01"]
result = server.cat.exec_case("DEMO-R01")
assert result["status"] == "passed", result
assert not [name for name in sys.modules if name.startswith("packs.sample_project.adapter")]
sys.path.insert(0, "scripts")
from check_env import build_preflight_report
assert "EXAMPLE_PROJECT_TEST_SSH" not in str(build_preflight_report(env={}))
try:
    identity_pool.pool_path("sample_project")
    raise AssertionError("foreign pool accepted")
except ValueError:
    pass
try:
    import sample_project_auth
    raise AssertionError("foreign auth accepted")
except (RuntimeError, ModuleNotFoundError):
    pass
print("demo isolated")
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                env={'PATH': os.environ['PATH'], 'PLATFORM_ENV_FILE': '/dev/null', 'PLATFORM_PACKS': 'demo_pack'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'demo isolated')

    def test_catalog_rejects_duplicates_without_replacing_previous_indexes(self):
        catalog = importlib.import_module('catalog')
        original = catalog._CAT
        first = SimpleNamespace(SUITES=[{'id': 'same'}], CATALOG=[{'id': 'a'}], CAT={'a': {}})
        second = SimpleNamespace(SUITES=[{'id': 'other'}], CATALOG=[{'id': 'a'}], CAT={'a': {}})
        for field, pattern in (("CATALOG", "case id 重复"), ("SUITES", "suite id 重复"), ("CAT", "runner case id 重复")):
            second.SUITES, second.CATALOG, second.CAT = [{'id': 'other'}], [{'id': 'b'}], {'b': {}}
            setattr(second, field, getattr(first, field))
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, pattern):
                catalog._assign_modules([first, second])
            self.assertIs(catalog._CAT, original)

    def test_catalog_does_not_reuse_an_unowned_cached_module(self):
        catalog = importlib.import_module('catalog')
        source = ROOT / 'examples/demo_pack/readonly_cases.py'
        with patch.object(catalog, 'case_source_files', return_value=[source]), \
                patch.object(catalog, 'discover_case_module_names', return_value=['readonly_cases']), \
                patch.dict(sys.modules, {'readonly_cases': SimpleNamespace(__file__='/tmp/foreign_cases.py')}):
            with self.assertRaisesRegex(RuntimeError, "加载来源不属于"):
                catalog._load_case_modules()


if __name__ == '__main__':
    unittest.main()
