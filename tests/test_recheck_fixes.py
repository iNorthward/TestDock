"""Targeted regressions for the follow-up review, with synthetic inputs only."""
import contextvars
import gc
import importlib.util
import shutil
import sys
import tempfile
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'test-platform'), str(ROOT / 'test-platform/common'), str(ROOT / 'scripts')]
import api_client
import catalog
import server


class RecheckTests(unittest.TestCase):
    def test_generated_pack_docs_and_doctor_handle_unknown_smoke_id(self):
        from create_project_pack import create
        from project_doctor import diagnose
        for name in ('AGENT_OPERATIONS.md', 'QUICKSTART.md'):
            document = (ROOT / 'docs/onboarding' / name).read_text()
            self.assertNotIn('--pack my_app --mock-case DEMO-R01', document)
            self.assertIn('MY_APP-R01', document)
        integration = (ROOT / 'docs/onboarding/AGENT_INTEGRATION.md').read_text()
        self.assertNotIn('MockTransport 和一条 DEMO-R01', integration)
        self.assertIn('MockTransport 和一条 SAMPLE_APP-R01', integration)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in ('test-platform', 'scripts', 'examples', 'resources'):
                shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('platform_config.py', 'timezone_config.py', 'platform_protocol.py', 'platform_capabilities.py'):
                shutil.copyfile(ROOT / name, root / name)
            create('my_app', 'Synthetic application', 18802, root=root)
            failed = diagnose('my_app', root=root, mock_case='DEMO-R01')
            self.assertEqual(failed['status'], 'failed')
            catalog_check = next(item for item in failed['checks'] if item['name'] == 'catalog-adapter')
            smoke = next(item for item in failed['checks'] if item['name'] == 'mock-smoke')
            self.assertEqual(catalog_check['status'], 'passed')
            self.assertIn('案例不存在', smoke['detail'])
            self.assertIn('MY_APP-R01', smoke['detail'])
            self.assertNotIn('Traceback', smoke['detail'])
            self.assertNotIn('manifest', smoke['nextAction'])
            self.assertEqual(diagnose('my_app', root=root, mock_case='MY_APP-R01')['status'], 'ready')

    def test_manual_reload_is_nonblocking_and_pending_clears_after_success(self):
        lock = catalog.RegistryLock()
        with patch.object(server, '_CATALOG_LOCK', lock), \
                patch.object(server, '_CATALOG_LOADED_FINGERPRINTS', {'case':1}), \
                patch.object(server, '_catalog_fingerprints', return_value={'case':2}), \
                patch.object(server, 'CATALOG_REVISION', 7), \
                patch.object(catalog, 'reload_registry', return_value={'cases':1,'suites':1}) as reload, \
                patch.object(server, 'sync_case_module_aliases'):
            self.assertTrue(server.catalog_revision_payload()['reloadPending'])
            with lock.read():
                with self.assertRaises(server.CatalogReloadBusy):
                    server.reload_catalog_registry(reason='http-post')
                self.assertEqual(lock.waiting_writers, 0)
                reload.assert_not_called()
                self.assertEqual(server.CATALOG_REVISION, 7)
            result = server.reload_catalog_registry(reason='http-post')
            self.assertEqual(result['revision'], 8)
            self.assertFalse(server.catalog_revision_payload()['reloadPending'])
            reload.assert_called_once()

    def test_watcher_defers_without_blocking_catalog_or_new_execution(self):
        lock = catalog.RegistryLock()
        held, release = threading.Event(), threading.Event()

        def batch():
            with lock.read():
                held.set()
                release.wait(3)

        worker = threading.Thread(target=batch)
        worker.start()
        self.assertTrue(held.wait(1))
        state = {'fingerprints': {'case': 1}}
        try:
            with patch.object(catalog, 'REGISTRY_LOCK', lock), patch.object(server, '_CATALOG_LOCK', lock), \
                    patch.object(server, '_catalog_fingerprints', return_value={'case': 2}), \
                    patch.object(server, 'reload_catalog_registry', return_value={'revision': 2}) as reload:
                self.assertIsNone(server._poll_catalog_reload(state))
                reload.assert_not_called()
                self.assertEqual(state['fingerprints'], {'case': 1})
                self.assertEqual(lock.waiting_writers, 0)
                with ThreadPoolExecutor(1) as executor:
                    self.assertIn('cases', executor.submit(catalog.catalog).result(timeout=.5))
                    self.assertEqual(executor.submit(catalog.exec_case, 'UNKNOWN-REVIEW-ID').result(timeout=.5)['error'], '未知用例')
                release.set()
                worker.join(1)
                self.assertEqual(server._poll_catalog_reload(state), {'revision': 2})
                reload.assert_called_once_with(reason='watch')
                self.assertEqual(state['fingerprints'], {'case': 2})
                self.assertIsNone(server._poll_catalog_reload(state))
        finally:
            release.set()
            worker.join(1)

    def test_catalog_reads_remain_available_while_manual_reload_waits(self):
        lock = catalog.RegistryLock()
        started, finished = threading.Event(), threading.Event()

        def writer():
            started.set()
            with lock:
                finished.set()

        with patch.object(catalog, 'REGISTRY_LOCK', lock):
            with lock.read():
                worker = threading.Thread(target=writer)
                worker.start()
                self.assertTrue(started.wait(1))
                with lock.condition:
                    self.assertTrue(lock.condition.wait_for(lambda: lock.waiting_writers == 1, timeout=1))
                viewed = threading.Event()
                result = []
                def view():
                    result.append(catalog.catalog())
                    viewed.set()
                reader = threading.Thread(target=view, daemon=True)
                reader.start()
                self.assertTrue(viewed.wait(.5), '目录不应等待批次释放读租约')
                self.assertIn('cases', result[0])
                self.assertFalse(finished.is_set())
            worker.join(1)
            reader.join(1)
            self.assertTrue(finished.is_set())

    def test_token_context_does_not_retain_disposed_sessions(self):
        def operation():
            references = []
            for _ in range(300):
                client = api_client.ApiSession(base='https://synthetic.example')
                client._token = 'synthetic-token'
                references.append(weakref.ref(client))
            del client
            gc.collect()
            self.assertTrue(all(reference() is None for reference in references))
            self.assertEqual(len(api_client._SESSION_TOKENS.get()), 0)
        contextvars.Context().run(operation)

    def test_copied_contexts_do_not_change_parent_token_or_identity(self):
        client = api_client.ApiSession(base='https://synthetic.example')
        client._token = 'parent'
        client._token_user = 'parent-user'
        copied = contextvars.copy_context()
        copied.run(setattr, client, '_token', 'child')
        copied.run(setattr, client, '_token_user', 'child-user')
        self.assertEqual((client._token, client._token_user), ('parent', 'parent-user'))
        self.assertEqual(copied.run(lambda: (client._token, client._token_user)), ('child', 'child-user'))

    def test_provider_cache_survives_new_thread_and_named_session_context(self):
        spec = importlib.util.spec_from_file_location('review_auth_recipe', ROOT / 'examples/adapter_recipes/auth_cache.py')
        recipe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recipe)
        login = Mock(return_value={'access_token': 'synthetic-token', 'expires_in': 120})
        provider = recipe.CachedLoginTokens(login, max_ttl=120)
        named = api_client._ContextSession(lambda: api_client.ApiSession(
            base='https://synthetic.example', default_user='reader', token_provider=provider))
        with ThreadPoolExecutor(2) as executor:
            # Explicit empty contexts model separate HTTP worker threads.
            results = [executor.submit(contextvars.Context().run, named.ensure_token) for _ in range(4)]
            self.assertEqual([result.result() for result in results], ['synthetic-token'] * 4)
        login.assert_called_once_with('reader', '')

    def test_documented_api_curl_examples_require_session_header(self):
        document = (ROOT / 'docs/onboarding/AGENT_INTEGRATION.md').read_text()
        examples = [line for line in document.splitlines() if line.lstrip().startswith(('curl ', 'TOKEN=$(curl ')) and '/api/' in line]
        self.assertGreaterEqual(len(examples), 4)
        for line in examples:
            if '/api/session' not in line:
                self.assertIn('X-TestDock-Token: $TOKEN', line)
        self.assertIn('SAMPLE_APP-R01', document)

    def test_ci_is_not_shipped_or_enabled(self):
        self.assertFalse((ROOT / '.github/workflows/check.yml').exists())
        self.assertNotIn('.github/workflows/check.yml', (ROOT / 'scripts/export_platform.py').read_text())


if __name__ == '__main__':
    unittest.main()
