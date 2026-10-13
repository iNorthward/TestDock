"""Owned process/queue shutdown tests; no real business service or signals."""
import os
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'test-platform'), str(ROOT / 'test-platform/common')]
import execution_jobs as jobs
from execution_queue import BoundedExecutor, QueueFull
import server


class ExecutionShutdownTests(unittest.TestCase):
    def test_body_timeout_reports_incomplete_before_dispatch(self):
        for exception in (TimeoutError('synthetic timeout'), socket.timeout('synthetic socket timeout')):
            handler = Mock(headers={'Content-Length': '10'}, close_connection=False)
            handler.rfile.read.side_effect = exception
            with self.subTest(exception=type(exception).__name__), self.assertRaises(server.RequestRejected) as caught:
                server.H._body(handler)
            self.assertEqual(caught.exception.status, 408)
            self.assertTrue(handler.close_connection)

    def test_shutdown_can_retry_after_a_temporary_storage_failure(self):
        httpd = object.__new__(server.PanelHTTPServer)
        httpd._lifecycle_condition = threading.Condition()
        httpd._active_mutations = 0
        httpd._stopping = False
        httpd._shutdown_thread = None
        httpd.shutdown = Mock()
        with patch.object(jobs, 'begin_shutdown', side_effect=[RuntimeError('token=synthetic-secret'), None]), \
             patch.object(jobs, 'wait_for_shutdown') as wait:
            with self.assertLogs(server.__name__, level='ERROR') as logs:
                httpd.request_shutdown()
                httpd.wait_for_shutdown()
            self.assertNotIn('synthetic-secret', logs.output[0])
            self.assertEqual(httpd._shutdown_error, 'token=[redacted]')
            httpd.shutdown.assert_not_called()
            with httpd.mutation_slot() as allowed:
                self.assertFalse(allowed)
            httpd.request_shutdown()
            httpd.wait_for_shutdown()
        wait.assert_called_once()
        httpd.shutdown.assert_called_once()
        self.assertIsNone(httpd._shutdown_error)

    def test_queued_worker_cannot_start_in_the_shutdown_cancel_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = jobs.JobStore(Path(directory) / 'jobs.db')
            executor = BoundedExecutor(1, 4)
            entered, release = threading.Event(), threading.Event()
            calls = []
            original_shutdown = executor.shutdown
            def first(*_arguments):
                entered.set()
                release.wait(3)
                return {'ok': True}
            try:
                with patch.object(jobs, 'get_store', return_value=store), \
                     patch.object(jobs, '_EXECUTOR', executor), patch.object(jobs, '_EXECUTOR_PID', os.getpid()), \
                     patch.object(jobs, '_SHUTTING_DOWN_PID', None):
                    jobs.start_background_job(kind='running', payload={}, operation=first)
                    self.assertTrue(entered.wait(1))
                    queued = jobs.start_background_job(kind='queued', payload={}, operation=lambda *_: calls.append('entered'))
                    def window(wait=True):
                        original_shutdown(wait=False)
                        release.set()
                        deadline = time.monotonic() + 2
                        while store.get(queued)['status'] not in jobs.TERMINAL and time.monotonic() < deadline:
                            time.sleep(.005)
                        # Persistent bulk cancellation has not yet run here.
                        self.assertEqual(store.get(queued)['status'], 'cancelled')
                    with patch.object(executor, 'shutdown', side_effect=window):
                        jobs.begin_shutdown()
                    jobs.wait_for_shutdown()
                    self.assertEqual(calls, [])
            finally:
                release.set()
                original_shutdown()

    def test_stop_cancels_only_owned_jobs_and_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = jobs.JobStore(Path(directory) / 'jobs.db')
            executor = BoundedExecutor(1, 8)
            entered, cleaning, release, cleaned, drained = [threading.Event() for _ in range(5)]
            calls = []
            def operation(*_arguments):
                entered.set()
                try:
                    while True:
                        jobs.cooperative_sleep(.01)
                finally:
                    with jobs.cleanup_context():
                        cleaning.set()
                        if not release.wait(3):
                            raise RuntimeError('synthetic cleanup was not released')
                        cleaned.set()
            try:
                with patch.object(jobs, 'get_store', return_value=store), \
                     patch.object(jobs, '_EXECUTOR', executor), patch.object(jobs, '_EXECUTOR_PID', os.getpid()), \
                     patch.object(jobs, '_SHUTTING_DOWN_PID', None):
                    running = jobs.start_background_job(kind='running', payload={}, operation=operation)
                    self.assertTrue(entered.wait(1))
                    queued = jobs.start_background_job(kind='queued', payload={}, operation=lambda *_: calls.append('queued'))
                    foreign, _ = store.create(kind='foreign')
                    with store.connection() as connection:
                        connection.execute('UPDATE execution_jobs SET owner_pid=? WHERE id=?', (99999999, foreign['id']))
                    jobs.begin_shutdown()
                    self.assertTrue(cleaning.wait(1))
                    self.assertEqual(store.get(queued)['status'], 'cancelled')
                    self.assertEqual(store.get(foreign['id'])['status'], 'queued')
                    with self.assertRaises(QueueFull):
                        jobs.get_executor()
                    refused = jobs.start_background_job(kind='new', payload={}, operation=lambda *_: calls.append('new'))
                    self.assertEqual(store.get(refused)['status'], 'incomplete')
                    waiter = threading.Thread(target=lambda: (jobs.wait_for_shutdown(), drained.set()), daemon=True)
                    waiter.start()
                    self.assertFalse(drained.wait(.05))
                    release.set()
                    self.assertTrue(drained.wait(2))
                    waiter.join(1)
                    self.assertTrue(cleaned.is_set())
                    self.assertEqual(store.get(running)['status'], 'cancelled')
                    self.assertEqual(calls, [])
            finally:
                release.set()
                executor.shutdown()

    def test_executor_shutdown_waits_for_a_synchronous_slot(self):
        executor = BoundedExecutor(1, 2)
        entered, release, drained = [threading.Event() for _ in range(3)]
        def synchronous():
            with executor.synchronous_slot():
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=synchronous)
        waiter = threading.Thread(target=lambda: (executor.shutdown(), drained.set()))
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            waiter.start()
            self.assertFalse(drained.wait(.05))
            release.set()
            self.assertTrue(drained.wait(1))
        finally:
            release.set()
            thread.join(2)
            waiter.join(2)
            executor.shutdown()

    def test_panel_stops_admission_and_waits_for_inflight_mutations(self):
        # Exercise lifecycle without opening a listening socket in offline tests.
        httpd = object.__new__(server.PanelHTTPServer)
        httpd._lifecycle_condition = threading.Condition()
        httpd._active_mutations = 0
        httpd._stopping = False
        httpd._shutdown_thread = None
        httpd.shutdown = Mock()
        begun = threading.Event()
        with patch.object(jobs, 'begin_shutdown', side_effect=begun.set), \
             patch.object(jobs, 'wait_for_shutdown') as drain:
            with httpd.mutation_slot() as allowed:
                self.assertTrue(allowed)
                httpd.request_shutdown()
                self.assertTrue(begun.wait(1))
                httpd.request_shutdown()
                with httpd.mutation_slot() as next_allowed:
                    self.assertFalse(next_allowed)
                httpd.shutdown.assert_not_called()
                drain.assert_not_called()
            httpd.wait_for_shutdown()
        drain.assert_called_once()
        httpd.shutdown.assert_called_once()

    def test_stop_script_retains_pid_and_never_force_kills_unfinished_cleanup(self):
        text = (ROOT / 'start.sh').read_text()
        start = text.index('stop_one() {')
        function = text[start:text.index('status_one() {', start)]
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'owned.pid'
            pidfile.write_text('123')
            code = '''
is_our_process() { return 0; }
port_pids() { printf 123; }
wait_down() { return 1; }
kill() { if [[ "$1" == "-9" ]]; then printf forced >&2; return 99; fi; return 0; }
''' + function + '\nstop_one synthetic 18899 test-platform/server.py "$FIXTURE_PID"\n'
            result = subprocess.run(['bash', '-c', code], env={'PATH': os.environ['PATH'], 'FIXTURE_PID': str(pidfile)},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('未强制终止', result.stderr)
            self.assertNotIn('forced', result.stderr)
            self.assertTrue(pidfile.is_file())


@unittest.skipIf(os.environ.get('PLATFORM_OFFLINE_CONTEXT'), 'owned loopback IO checked separately')
class ShutdownLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.replacements = [patch.object(jobs, 'begin_shutdown'), patch.object(jobs, 'wait_for_shutdown'),
                             patch.object(server.H, 'io_timeout_seconds', .2)]
        for replacement in self.replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.httpd = server.PanelHTTPServer(('127.0.0.1', 0), server.H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.httpd.request_shutdown()
        self.httpd.wait_for_shutdown()
        self.thread.join(2)
        self.httpd.server_close()

    def test_partial_authenticated_body_does_not_hold_shutdown_forever(self):
        stream = socket.create_connection(self.httpd.server_address, timeout=1)
        self.addCleanup(stream.close)
        port = self.httpd.server_port
        headers = (f'POST /api/run-case HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n'
                   f'X-TestDock-Token: {self.httpd.session_token}\r\nContent-Type: application/json\r\n'
                   'Content-Length: 100\r\n\r\n{')
        stream.sendall(headers.encode())
        deadline = time.monotonic() + 1
        while not self.httpd._active_mutations and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.httpd._active_mutations, 1)
        self.httpd.request_shutdown()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.httpd._active_mutations, 0)
        with stream.makefile('rb') as response_file:
            response = response_file.read()
        self.assertIn(b'408 Request Timeout', response)
        self.assertIn(b'"status": "incomplete"', response)

    def test_stopping_panel_returns_real_503_incomplete_without_starting_work(self):
        # Keep the serve loop alive long enough to read the stopping response.
        with self.httpd._lifecycle_condition:
            self.httpd._stopping = True
        stream = socket.create_connection(self.httpd.server_address, timeout=1)
        self.addCleanup(stream.close)
        port = self.httpd.server_port
        body = b'{"id":"DEMO-R01"}'
        stream.sendall((f'POST /api/run-case HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n'
                       f'X-TestDock-Token: {self.httpd.session_token}\r\nContent-Type: application/json\r\n'
                       f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
        with stream.makefile('rb') as response_file:
            response = response_file.read()
        self.assertIn(b'503 Service Unavailable', response)
        payload = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertIsNone(payload['ok'])
        self.assertEqual(payload['status'], 'incomplete')

    def test_sigterm_waits_for_owned_pack_cleanup_and_persists_cancelled_job(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        from create_project_pack import create
        from export_platform import build
        with tempfile.TemporaryDirectory(prefix='testdock-signal-') as directory:
            temporary = Path(directory)
            build(temporary / 'release')
            root = temporary / 'release/platform'
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            pack_root = create('shutdown_probe', 'Shutdown probe', port, root=root)
            manifest = json.loads((pack_root / 'pack.json').read_text())
            case_path = next((root / manifest['case_dirs'][0]).glob('*_cases.py'))
            case_path.write_text('''import os,time
from pathlib import Path
from execution_jobs import cooperative_sleep,cleanup_context
SUITES=[{"id":"probe","name":"Owned shutdown probe","parent":"probe"}]
CATALOG=[{"id":"PROBE-W01","suite":"probe","mode":"live","desc":"Temporary synthetic cleanup","requires_write_opt_in":True}]
CAT={"PROBE-W01":True}
def exec_case(case_id):
    marker=Path(os.environ["SYNTHETIC_MARKER_ROOT"])
    (marker/"entered").write_text("owned synthetic operation")
    try:
        while True:cooperative_sleep(.01)
    finally:
        with cleanup_context():
            (marker/"cleaning").write_text("owned synthetic cleanup")
            deadline=time.monotonic()+8
            while not (marker/"release").exists() and time.monotonic()<deadline:time.sleep(.01)
            if not (marker/"release").exists():raise RuntimeError("synthetic cleanup release missing")
            (marker/"cleaned").write_text("cleanup finished")
''')
            markers = temporary / 'markers'
            markers.mkdir()
            environment = {key: os.environ[key] for key in ('PATH', 'TMPDIR', 'LANG') if key in os.environ}
            environment.update(PLATFORM_ENV_FILE='/dev/null', PLATFORM_PACKS='shutdown_probe',
                PLATFORM_PANEL_PORT=str(port), PLATFORM_PACK_DATA_DIR=str(temporary / 'data'),
                PLATFORM_ARTIFACT_ROOT=str(temporary / 'artifacts'), PLATFORM_ALLOW_LIVE='1',
                PLATFORM_ENABLE_WRITE_CASE='PROBE-W01', SYNTHETIC_MARKER_ROOT=str(markers),
                PYTHONDONTWRITEBYTECODE='1')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = 'http://127.0.0.1:' + str(port)
            with (temporary / 'panel.log').open('w') as log:
                process = subprocess.Popen([sys.executable, str(root / 'test-platform/server.py'),
                    '--pack=shutdown_probe', '--port=' + str(port)], cwd=root, env=environment, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 8
                    token = None
                    while time.monotonic() < deadline:
                        try:
                            with opener.open(base + '/api/session', timeout=.5) as response:
                                token = json.load(response)['token']
                            break
                        except OSError:
                            if process.poll() is not None:
                                self.fail('Owned panel startup failed: ' + Path(log.name).read_text()[-1500:])
                            time.sleep(.02)
                    self.assertIsNotNone(token)
                    def submit(request_id):
                        body = json.dumps({'id': 'PROBE-W01', 'confirmLive': 'PROBE-W01',
                                           'background': True, 'requestId': request_id}).encode()
                        request = urllib.request.Request(base + '/api/run-case', data=body,
                            headers={'Content-Type': 'application/json', 'X-TestDock-Token': token})
                        return opener.open(request, timeout=2)
                    with submit('owned-signal') as response:
                        identifier = json.load(response)['data']['executionId']
                    deadline = time.monotonic() + 3
                    while not (markers / 'entered').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((markers / 'entered').exists())
                    process.terminate()
                    deadline = time.monotonic() + 3
                    while not (markers / 'cleaning').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((markers / 'cleaning').exists())
                    self.assertIsNone(process.poll())
                    self.assertFalse((markers / 'cleaned').exists())
                    with self.assertRaises(urllib.error.HTTPError) as refusal:
                        submit('owned-while-stopping')
                    self.assertEqual(refusal.exception.code, 503)
                    with refusal.exception as response:
                        self.assertEqual(json.load(response)['status'], 'incomplete')
                    (markers / 'release').write_text('finish owned synthetic cleanup')
                    self.assertEqual(process.wait(timeout=4), 0)
                    self.assertTrue((markers / 'cleaned').exists())
                    connection = sqlite3.connect('file:' + str(temporary / 'data/execution_jobs.db') + '?mode=ro', uri=True)
                    try:
                        self.assertEqual(connection.execute('SELECT status FROM execution_jobs WHERE id=?', (identifier,)).fetchone()[0], 'cancelled')
                    finally:
                        connection.close()
                finally:
                    (markers / 'release').touch()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            # Only this disposable synthetic process is owned by the test.
                            process.kill()
                            process.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
