"""Focused regression for the reviewed correctness and security defects."""
import contextlib
import http.client
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'test-platform'), str(ROOT/'test-platform/common'), str(ROOT/'scripts')]
import api_client
import catalog
import execution_jobs as jobs
import http_security
import identity_pool
import run_history
import server
import offline_process


class PolicyTests(unittest.TestCase):
    def test_cross_site_host_token_and_body_policy(self):
        valid = {'Host':'localhost:8801', 'X-TestDock-Token':'test-token', 'Content-Type':'application/json'}
        http_security.authorize(valid, port=8801, token='test-token', api=True, mutation=True)
        for changed, code in [({'Host':'evil.example'},403), ({'Origin':'http://evil.example'},403),
                              ({'Referer':'http://localhost:8802/'},403), ({'X-TestDock-Token':''},403),
                              ({'Sec-Fetch-Site':'cross-site'},403), ({'Content-Type':'text/plain'},415),
                              ({'Content-Length':'1048577'},413), ({'Content-Length':'-1'},400),
                              ({'Transfer-Encoding':'chunked'},400)]:
            with self.subTest(changed=changed), self.assertRaises(http_security.RequestRejected) as caught:
                http_security.authorize({**valid,**changed},port=8801,token='test-token',api=True,mutation=True)
            self.assertEqual(caught.exception.status,code)

    def test_mode_and_live_authorization_before_dispatch(self):
        cases={'WRITE':{'id':'WRITE','mode':'live'},'READ':{'id':'READ','mode':'safe'},
               'WRITE-OPT':{'id':'WRITE-OPT','mode':'safe','requires_write_opt_in':True},
               'UNSCOPED-OPT':{'id':'UNSCOPED-OPT','mode':'safe','requires_unscoped_write_opt_in':True}}
        with patch.object(catalog,'_CAT',cases), patch.dict(os.environ,{},clear=True):
            for data in ({'mode':'liv','id':'READ'}, {'id':'WRITE'}, {'id':'WRITE','confirmLive':'WRITE'}):
                with self.assertRaises(ValueError): server._validate_panel_execution(data,batch=False)
            server._validate_panel_execution({'id':'READ'},batch=False)
            for identifier in ('WRITE-OPT', 'UNSCOPED-OPT'):
                with self.assertRaises(ValueError):
                    server._validate_panel_execution({'id':identifier,'confirmLive':identifier},batch=False)
            with patch.dict(os.environ,{'PLATFORM_ALLOW_LIVE':'1'}):
                with self.assertRaises(ValueError): server._validate_panel_execution({'id':'WRITE','confirmLive':'READ'},batch=False)
                server._validate_panel_execution({'id':'WRITE','confirmLive':'WRITE'},batch=False)
                for identifier in ('WRITE-OPT', 'UNSCOPED-OPT'):
                    with self.assertRaises(ValueError):
                        server._validate_panel_execution({'id':identifier},batch=False)
                    server._validate_panel_execution({'id':identifier,'confirmLive':identifier},batch=False)
        with self.assertRaises(ValueError): catalog.run_all('liv')

    def test_cross_origin_session_rejects_before_authentication_and_strips_opt_in_headers(self):
        provider=Mock();auth=Mock();transport=Mock()
        transport.request.return_value=(200,{})
        client=api_client.ApiSession(base='https://owned.example',token_provider=provider,request_auth=auth,transport=transport)
        with self.assertRaises(ValueError):client.get('https://evil.example')
        provider.get_token.assert_not_called();transport.request.assert_not_called()
        client.get('https://evil.example',allow_external=True,token='sensitive')
        self.assertEqual(transport.request.call_args.args[2],{'Content-Type':'application/json'})
        auth.build_headers.assert_not_called();provider.get_token.assert_not_called()
        with self.assertRaises(ValueError):client.get('https://evil.example',allow_external=True,extra_headers={'Authorization':'secret'})

    def test_cross_origin_redirect_is_blocked(self):
        request=api_client.urllib.request.Request('https://owned.example/a',headers={'Authorization':'Bearer secret'})
        with self.assertRaises(api_client.urllib.error.URLError):
            api_client.SameOriginRedirect().redirect_request(request,None,302,'Found',{},'https://evil.example/a')

    def test_timeout_is_transport_failure(self):
        with patch.object(api_client.OPENER,'open',side_effect=TimeoutError), patch.object(api_client,'_REQUEST_AUTH',None):
            status,result=api_client.http('GET','https://owned.example',{})
        self.assertEqual(status,0);self.assertTrue(result['_transport_error'])

    def test_session_tokens_do_not_cross_threads(self):
        session=api_client.ApiSession(base='https://owned.example')
        session._token='main'
        with ThreadPoolExecutor(1) as executor:
            def worker():
                self.assertIsNone(session._token)
                session._token='worker'
                return session._token
            self.assertEqual(executor.submit(worker).result(),'worker')
        self.assertEqual(session._token,'main')

    def test_reload_waits_for_batch_registry_lock(self):
        entered=threading.Event();finished=threading.Event()
        def reload():
            entered.set();catalog.reload_registry();finished.set()
        with patch.object(catalog,'_load_case_modules',return_value=[]),patch.object(catalog,'_assign_modules'),patch.object(catalog,'_call_catalog_extensions'):
            with catalog.REGISTRY_LOCK:
                worker=threading.Thread(target=reload);worker.start()
                self.assertTrue(entered.wait(2));self.assertFalse(finished.wait(.05))
            worker.join(2)
            self.assertTrue(finished.is_set())

    def test_synchronous_without_request_id_enforces_cooperative_deadline(self):
        with tempfile.TemporaryDirectory() as td,patch.object(jobs,'get_store',return_value=jobs.JobStore(Path(td)/'jobs.db')):
            def operation():
                control=jobs._CONTROL.get()
                self.assertIsNotNone(control)
                control.deadline=0
                jobs.checkpoint()
            result=jobs.run_idempotent(kind='test',request_id=None,payload={},operation=operation,timeout_seconds=1)
            self.assertEqual(result['executionStatus'],'timed_out');self.assertIsNone(result['ok'])

    def test_history_redacts_credentials_closes_connections_and_preserves_four_states(self):
        with tempfile.TemporaryDirectory() as td,patch.object(run_history,'DB_PATH',Path(td)/'history.db'):
            rid=run_history.record_case({'id':'TEST','ok':None,'status':'incomplete','subs':[{'ok':None,'status':'incomplete','detail':'Bearer sensitive token=secret','password':'hidden'}]})
            raw=run_history.DB_PATH.read_bytes()
            for secret in (b'sensitive',b'secret',b'hidden'):self.assertNotIn(secret,raw)
            detail=run_history.run_detail(rid)
            self.assertIsNone(detail['cases'][0]['ok'])
            self.assertIsNone(run_history.case_history('TEST')[0]['ok'])
            with run_history._conn() as connection: pass
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            with patch.object(run_history,'_migrate',side_effect=AssertionError('migrated twice')):
                run_history.recent_runs()
            with self.assertRaises(ValueError):run_history.run_detail('abc')

    def test_password_reference_resolves_only_at_use(self):
        with patch.dict(os.environ,{'TEST_PASSWORD':'synthetic-password'}):
            result=identity_pool.identity_with_password({'active':True,'category':'user','role':'reader','password_ref':'env:TEST_PASSWORD'})
        self.assertEqual(result['password'],'synthetic-password')
        safe=identity_pool.redact_secrets(result)
        self.assertNotIn('password',safe);self.assertNotIn('password_ref',safe)
        with patch.dict(os.environ,{},clear=True),self.assertRaises(identity_pool.IdentityUnavailable):
            identity_pool._password_reference('env:TEST_PASSWORD')
        with self.assertRaises(ValueError):identity_pool._password_reference('file:/etc/passwd')

    def test_missing_pack_tools_fail_friendly_without_traceback(self):
        for name in ('pack_tool.py', 'error_log_audit.py', 'migrate_identity_pool.py'):
            arguments = ['missing_alias'] if name == 'pack_tool.py' else []
            result = subprocess.run([sys.executable, str(ROOT/'scripts'/name), *arguments],
                                    env={**os.environ, 'PLATFORM_ENV_FILE':'/dev/null', 'PLATFORM_PACKS':'demo_pack'},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('[incomplete]', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_contract_credentials_require_declared_project_keys(self):
        import fetch_apifox_oas
        import platform_config
        with patch.object(fetch_apifox_oas, '_load_env_local'), \
                patch.object(platform_config, '_PROJECT_ALIASES', {}), \
                patch.object(platform_config, '_PROJECT_DEFAULTS', {}), \
                patch.dict(os.environ, {'APIFOX_PROJECT_ID':'synthetic-project', 'APIFOX_TOKEN':'undeclared-secret'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'APIFOX_ACCESS_TOKEN'):
                fetch_apifox_oas.resolve_credentials()
            self.assertFalse(hasattr(fetch_apifox_oas, '_credentials_from_cursor_mcp'))

    def test_bubblewrap_rebinds_tmp_checkout_readonly_after_tmpfs(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);profile=folder/'sandbox.sb'
            (folder/'context.json').write_text(json.dumps({'protected_roots':['/tmp/probe','/tmp/core'],'protected_reads':[]}))
            with patch.object(offline_process.offline_runtime,'installed',return_value=False),patch.object(offline_process.sys,'platform','linux'),patch.object(offline_process.shutil,'which',return_value='/usr/bin/bwrap'):
                command,_=offline_process.system_command(['python','/tmp/probe/test.py'],profile)
            self.assertGreater(command.index('/tmp/probe'),command.index('--tmpfs'))
            self.assertIn(['--ro-bind','/tmp/probe','/tmp/probe'],[command[i:i+3] for i in range(len(command)-2)])


@unittest.skipIf(os.environ.get('PLATFORM_OFFLINE_CONTEXT'), 'real loopback HTTP verified separately outside offline sandbox')
class RealPanelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.httpd=server.PanelHTTPServer(('127.0.0.1',0),server.H)
        thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.httpd.server_close);self.addCleanup(self.httpd.shutdown)
        self.token=self.httpd.session_token

    def request(self,path,method='GET',body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3)
        self.addCleanup(connection.close)
        base={'X-TestDock-Token':self.token,'Content-Type':'application/json'}
        connection.request(method,path,body=body,headers={**base,**(headers or {})})
        response=connection.getresponse();raw=response.read()
        return response.status,raw

    def test_http_attack_requests_never_reach_runner(self):
        with patch.object(server,'_run_panel_request') as run:
            for headers,status in [({'Origin':'http://evil.example'},403),({'Host':'evil.example'},403),
                                   ({'X-TestDock-Token':''},403),({'Content-Type':'text/plain'},415),
                                   ({'Content-Length':'1048577'},413)]:
                code,_=self.request('/api/run-case','POST','{}',headers)
                self.assertEqual(code,status)
            run.assert_not_called()

    def test_valid_page_session_read_and_post(self):
        code,page=self.request('/')
        self.assertEqual(code,200);self.assertIn(b'/assets/security-client.js',page)
        code,body=self.request('/api/session',headers={'X-TestDock-Token':''})
        self.assertEqual(json.loads(body)['token'],self.token)
        self.assertEqual(self.request('/api/platform')[0],200)
        with patch.object(server,'_run_panel_request',return_value={'status':'passed'}) as run:
            self.assertEqual(self.request('/api/run-case','POST','{"id":"DEMO-R01"}')[0],200)
            run.assert_called_once()

    def test_get_mutations_bad_history_and_invalid_mode_are_rejected(self):
        with patch.object(server,'reload_catalog_registry') as reload:
            for path in ('/api/catalog/reload','/api/catalog?reload=1','/api/db-schema/patrol-stream','/api/db-schema/sync-stream'):
                self.assertEqual(self.request(path)[0],405)
            reload.assert_not_called()
        self.assertEqual(self.request('/api/history/run?id=abc')[0],400)
        self.assertEqual(self.request('/api/run','POST','{"mode":"liv"}')[0],400)
        self.assertEqual(self.request('/api/run','POST','{"mode":"live"}')[0],400)
        self.assertEqual(self.request('/api/apifox/reports')[0],404)
        self.assertEqual(self.request('/api/run','POST','[]')[0],400)

    def test_authenticated_optional_error_log_stream_dispatch(self):
        calls = []
        class SyntheticExtension:
            def dispatch(extension, method, handler, path, query, body):
                if path != '/api/error-log/run-stream':
                    return False
                calls.append((method, query))
                payload = b'data: {"type":"complete","result":{"ok":true}}\n\n'
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/event-stream')
                handler.send_header('Content-Length', str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
                return True
        with patch.object(server, 'PANEL_EXTENSIONS', [('synthetic', SyntheticExtension())]):
            url = '/api/error-log/run-stream?env=synthetic&from=2026-10-10&to=2026-10-11'
            self.assertEqual(self.request(url, headers={'X-TestDock-Token':''})[0], 403)
            self.assertEqual(calls, [])
            code, stream = self.request(url)
            self.assertEqual(code, 200)
            self.assertIn(b'"type":"complete"', stream)
            self.assertEqual(calls, [('GET', {'env':['synthetic'], 'from':['2026-10-10'], 'to':['2026-10-11']})])

    def test_documented_curl_session_and_api_flow(self):
        document = (ROOT / 'docs/onboarding/AGENT_INTEGRATION.md').read_text()
        section = document.split('## 14.')[1].split('```sh\n', 1)[1].split('```', 1)[0]
        script = section.replace('http://localhost:8802', f'http://127.0.0.1:{self.httpd.server_port}')
        script = script.replace('SAMPLE_APP-R01', 'DEMO-R01')
        with patch.object(server, '_run_panel_request', return_value={'status':'passed'}) as run:
            result = subprocess.run(['bash', '-e', '-c', script], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('403', result.stdout)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0]['id'], 'DEMO-R01')

    def test_manual_reload_busy_returns_409_without_queuing_writer(self):
        lock = catalog.RegistryLock()
        with patch.object(server, '_CATALOG_LOCK', lock), \
                patch.object(catalog, 'reload_registry', return_value={'cases':1,'suites':1}) as reload, \
                patch.object(server, 'sync_case_module_aliases'):
            with lock.read():
                code, body = self.request('/api/catalog/reload', 'POST', '{}')
                self.assertEqual(code, 409)
                data = json.loads(body)
                self.assertIsNone(data['ok'])
                self.assertEqual(data['status'], 'incomplete')
                self.assertEqual(lock.waiting_writers, 0)
                reload.assert_not_called()
            self.assertEqual(self.request('/api/catalog/reload', 'POST', '{}')[0], 200)
