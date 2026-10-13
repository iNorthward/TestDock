"""Synthetic regressions for matching execution gates and retained stop evidence."""
import os
import io
import json
import logging
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'test-platform'), str(ROOT / 'test-platform/common')]
import catalog
import execution_guard
import execution_jobs as jobs
from execution_queue import BoundedExecutor
import run_history as history
import server


class ExecutionSafetyFixes(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = jobs.JobStore(self.directory / 'jobs.db')
        self.executor = BoundedExecutor(1, 8)
        self.addCleanup(self.executor.shutdown)
        for replacement in (
            patch.object(jobs, 'get_store', return_value=self.store),
            patch.object(jobs, 'get_executor', return_value=self.executor),
            patch.object(history, 'DB_PATH', self.directory / 'history.db'),
            patch.object(execution_guard, 'lock_directory', return_value=self.directory / 'locks'),
            patch.object(server, 'execution_metadata', return_value={'packId': 'synthetic'}),
            patch.object(catalog, 'CATALOG_EXTENSIONS', []),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    @contextmanager
    def cases(self, values, runner):
        module = ModuleType('synthetic_cases')
        module.exec_case = Mock(side_effect=runner)
        module.ensure_token = Mock()
        with patch.object(catalog, '_ALL', values), \
             patch.object(catalog, '_CAT', {case['id']: case for case in values}), \
             patch.object(catalog, '_EXEC', {case['id']: module for case in values}), \
             patch.object(catalog, 'MODULES', [module]), \
             patch.object(catalog, 'SUITES', [{'id': name} for name in {c['suite'] for c in values}]):
            yield module

    def wait(self, identifier):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.store.get(identifier)
            if job['status'] in jobs.TERMINAL:
                return job
            time.sleep(.005)
        self.fail('Synthetic job did not reach a terminal state')

    def test_safe_batch_write_markers_require_the_same_panel_opt_in(self):
        for index, marker in enumerate(('requires_write_opt_in', 'requires_unscoped_write_opt_in')):
            case = {'id': 'S-W01', 'suite': 'synthetic', 'mode': 'safe', marker: True}
            opt_in = 'PLATFORM_ENABLE_WRITE_CASE' if marker == 'requires_write_opt_in' else 'PLATFORM_ENABLE_UNSCOPED_WRITE_CASE'
            with self.subTest(marker=marker), self.cases([case], lambda cid: {
                'id': cid, 'ok': True, 'subs': [{'ok': True, 'status': 'passed'}]} ) as module, \
                 patch.dict(os.environ, {opt_in: case['id'], 'PLATFORM_ALLOW_LIVE': ''}):
                body = {'mode': 'safe', 'suite': 'synthetic', 'requestId': 'gate-' + str(index)}
                with self.assertRaises(ValueError):
                    server._run_panel_request(body, batch=True)
                module.exec_case.assert_not_called()
                module.ensure_token.assert_not_called()
                with patch.dict(os.environ, {'PLATFORM_ALLOW_LIVE': '1'}):
                    with self.assertRaises(ValueError):
                        server._run_panel_request({**body, 'confirmLive': 'wrong'}, batch=True)
                    result = server._run_panel_request({**body, 'confirmLive': 'synthetic'}, batch=True)
                self.assertEqual(result['status'], 'passed')
                module.exec_case.assert_called_once()

    def test_gate_uses_only_the_actual_selected_case_set(self):
        values = [{'id': 'S-R01', 'suite': 'selected', 'mode': 'safe'},
                  {'id': 'S-W01', 'suite': 'selected', 'mode': 'live', 'requires_write_opt_in': True},
                  {'id': 'OTHER-W01', 'suite': 'other', 'mode': 'safe', 'requires_write_opt_in': True}]
        with self.cases(values, lambda cid: {}) , patch.dict(os.environ, {'PLATFORM_ALLOW_LIVE': ''}):
            server._validate_panel_execution({'mode': 'safe', 'suite': 'selected'}, batch=True)
            with self.assertRaises(ValueError):
                server._validate_panel_execution({'mode': 'safe'}, batch=True)
            with patch.dict(os.environ, {'PLATFORM_ALLOW_LIVE': '1'}):
                server._validate_panel_execution({'mode': 'safe', 'confirmLive': 'all'}, batch=True)

    def test_stopped_batch_retains_completed_cases_and_history(self):
        values = [{'id': cid, 'suite': 'synthetic', 'mode': 'safe'} for cid in ('S-R01', 'S-R02')]
        for reason in ('cancelled', 'timed_out'):
            for background in (False, True):
                calls = []
                def runner(cid):
                    calls.append(cid)
                    control = jobs._CONTROL.get()
                    if reason == 'cancelled':
                        self.store.cancel(control.job_id)
                    else:
                        control.deadline = 0
                    return {'id': cid, 'ok': True, 'subs': [{'label': 'completed', 'ok': True, 'status': 'passed'}]}
                with self.subTest(reason=reason, background=background), self.cases(values, runner):
                    submitted = server._run_panel_request({
                        'suite': 'synthetic', 'requestId': f'{reason}-{background}', 'background': background}, batch=True)
                    job = self.wait(submitted['executionId'])
                result = job['result']
                self.assertEqual(job['status'], reason)
                self.assertEqual(calls, ['S-R01'])
                self.assertEqual([(c['id'], c['status']) for c in result['cases']],
                                 [('S-R01', 'passed'), ('S-R02', 'incomplete')])
                self.assertTrue(result['historySaved'])
                stored = history.run_detail(result['runId'])
                self.assertEqual(stored['status'], 'incomplete')
                self.assertEqual(stored['executionMetadata']['executionStatus'], reason)
                self.assertEqual(stored['cases'][0]['status'], 'passed')
                self.assertEqual(len(stored['cases']), 2)

    def test_completed_failure_has_priority_over_batch_cancellation(self):
        values = [{'id': cid, 'suite': 'synthetic', 'mode': 'safe'} for cid in ('S-R01', 'S-R02')]
        def runner(cid):
            self.store.cancel(jobs._CONTROL.get().job_id)
            return {'id': cid, 'ok': False, 'subs': [{'label': 'failed assertion', 'ok': False, 'status': 'failed'}]}
        with self.cases(values, runner):
            result = server._run_panel_request({'suite': 'synthetic', 'requestId': 'failed-then-cancel'}, batch=True)
        job = self.store.get(result['executionId'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['result']['executionStatus'], 'cancelled')
        self.assertEqual(history.run_detail(result['runId'])['status'], 'failed')
        self.assertEqual(result['cases'][0]['status'], 'failed')

    def test_final_control_check_preserves_the_operation_result(self):
        for background in (False, True):
            def operation(*_arguments):
                self.store.cancel(jobs._CONTROL.get().job_id)
                return {'ok': True, 'status': 'passed', 'historySaved': True, 'runId': 42,
                        'cases': [{'id': 'S-R01', 'status': 'passed'}]}
            if background:
                identifier = jobs.start_background_job(kind='late-stop', request_id='late-background', payload={}, operation=operation)
            else:
                result = jobs.run_idempotent(kind='late-stop', request_id='late-sync', payload={}, operation=operation)
                identifier = result['executionId']
            job = self.wait(identifier)
            self.assertEqual(job['status'], 'cancelled')
            self.assertEqual(job['result']['cases'][0]['id'], 'S-R01')
            self.assertEqual(job['result']['runId'], 42)
            self.assertTrue(job['result']['historySaved'])

    def test_first_synchronous_response_is_redacted_like_retry_and_queries(self):
        marker = 'synthetic-audit-secret'
        operation = Mock(return_value={'ok': False, 'status': 'failed', 'error': 'token=' + marker,
                                      'password': marker, 'nested': {'accessToken': marker}})
        arguments = dict(kind='redaction', request_id='redaction-first', payload={}, operation=operation)
        first = jobs.run_idempotent(**arguments)
        retry = jobs.run_idempotent(**arguments)
        stored = self.store.get(first['executionId'])
        self.assertNotIn(marker, json.dumps([first, retry, stored]))
        self.assertNotIn('password', first)
        self.assertEqual(first['error'], 'token=[redacted]')
        operation.assert_called_once()

    def test_known_shutdown_response_keeps_incomplete_and_internal_error_log_is_redacted(self):
        handler = SimpleNamespace(request_id='synthetic', wfile=io.BytesIO(),
                                  send_response=Mock(), send_header=Mock(), end_headers=Mock())
        server.H._json(handler, 503, {'ok': None, 'status': 'incomplete', 'error': '服务正在等待任务清理'})
        payload = json.loads(handler.wfile.getvalue())
        self.assertIsNone(payload['ok'])
        self.assertEqual(payload['status'], 'incomplete')
        handler.wfile = io.BytesIO()
        with self.assertLogs(server.__name__, level='ERROR') as records:
            server.H._json(handler, 500, {'ok': False, 'error': 'token=synthetic-audit-secret'})
        self.assertNotIn('synthetic-audit-secret', records.output[0])
        self.assertNotIn('synthetic-audit-secret', handler.wfile.getvalue().decode())

    def test_log_formatter_redacts_exception_tracebacks(self):
        try:
            raise RuntimeError('token=synthetic-audit-secret')
        except RuntimeError:
            record = logging.LogRecord('synthetic', logging.ERROR, __file__, 1, 'failure', (), sys.exc_info())
            self.assertNotIn('synthetic-audit-secret', server.RedactingFormatter().format(record))

    def test_cancelled_run_with_all_completed_cases_still_records_incomplete_execution(self):
        identifier = history.record_run({'mode': 'safe', 'executionStatus': 'cancelled',
            'cases': [{'id': 'S-R01', 'ok': True, 'subs': [{'ok': True, 'status': 'passed'}]}]},
            metadata={'packId': 'synthetic'})
        run = history.run_detail(identifier)
        self.assertEqual(run['status'], 'incomplete')
        self.assertEqual((run['passed'], run['total']), (1, 1))
        self.assertEqual(run['cases'][0]['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
