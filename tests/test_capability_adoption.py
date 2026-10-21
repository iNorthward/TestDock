"""Capability declarations and evidence freshness, using owned synthetic files only."""
import json
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import platform_capabilities as capabilities
import project_doctor
import project_tasks


class CapabilityAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'resources').mkdir()
        (self.root / 'resources/platform-core-files.json').write_text('["core_sample.py"]')
        (self.root / 'core_sample.py').write_text('# synthetic Core source\n')
        (self.root / 'assets').mkdir()
        (self.root / 'assets/shared.js').write_text('// shared synthetic UI\n')
        for pack_id in ('sample', 'other'):
            folder = self.root / 'packs' / pack_id
            folder.mkdir(parents=True)
            (folder / 'integration.py').write_text('raise RuntimeError("must never be imported")\n')
            (folder / '__init__.py').write_text('# synthetic bootstrap source\n')
            (self.root / 'artifacts' / pack_id).mkdir(parents=True)
        self.manifest = {
            'id': 'sample', 'bootstrap': 'packs.sample:install',
            'adapter_files': ['packs/sample/integration.py'],
            'resource_pool': {'enabled': False}, 'diagnostic_capabilities': [],
            'config_defaults': {'PLATFORM_PANEL_PORT': '18802', 'PLATFORM_PACK_DATA_DIR': 'data/sample',
                                'PLATFORM_ARTIFACT_ROOT': 'artifacts/sample', 'PRIVATE_KEY': 'do-not-project-this-value'},
        }
        self.write_manifest()
        (self.root / '.env.sample').write_text('PLATFORM_PACKS=sample\n')

    def write_manifest(self):
        (self.root / 'packs/sample/pack.json').write_text(json.dumps(self.manifest))

    def declare(self, **items):
        self.manifest['capability_adoption'] = {'version': 1, 'items': items}
        self.write_manifest()

    def http(self, required=True, mode='core'):
        return {'mode': mode, 'required': required,
                'sources': ['adapter:packs/sample/integration.py', 'config:SERVICE_URL'],
                'reason': 'Dedicated Adapter policy' if mode == 'pack' else ''}

    def fact(self, capability='http', status='passed', scope='synthetic', **extra):
        return {'pack': 'sample', 'capability': capability,
                'ok': True if status == 'passed' else False if status == 'failed' else None,
                'status': status, 'scope': scope, 'verifiedAt': int(time.time()),
                'binding': capabilities.build_binding(self.manifest, root=self.root, capability=capability), **extra}

    def record(self, report=None, capability='http'):
        path = self.root / 'artifacts/sample' / (capability + '.json')
        path.write_text(json.dumps(report or self.fact(capability)))
        result = project_tasks.record_capability('sample', capability, path, root=self.root)
        return path, result

    def report(self):
        return capabilities.adoption_report(self.manifest, root=self.root)

    def row(self, report=None, capability='http'):
        return next(item for item in (report or self.report())['items'] if item['id'] == capability)

    def test_legacy_is_compatible_but_never_implicitly_verified(self):
        report = self.report()
        self.assertEqual(report['status'], 'ready')
        self.assertTrue(report['needsMigration'])
        self.assertFalse(report['declared'])
        row = self.row(report, 'resource_pool')
        self.assertEqual(row['mode'], 'disabled')
        self.assertFalse(row['declared'])
        self.assertIsNone(row['required'])
        self.assertEqual(row['verification']['status'], 'not_run')
        self.assertEqual(self.row(report)['mode'], 'undeclared')

    def test_legacy_external_owned_adapter_and_shared_panel_need_no_new_binding(self):
        adapter=self.root/'legacy_adapter.py'
        adapter.write_text('# legacy explicitly owned Adapter\n')
        panel=self.root/'test-platform/panel.html'
        panel.parent.mkdir()
        panel.write_text('<p>shared Core panel</p>')
        self.manifest.update(adapter_files=['legacy_adapter.py'],panel_html='test-platform/panel.html')
        self.write_manifest()
        original=Path.read_bytes
        def owned_only(path):
            if path in {adapter,panel}:
                raise AssertionError('An undeclared legacy capability must not bind new Adapter sources')
            return original(path)
        with patch.object(Path,'read_bytes',owned_only):
            report=self.report()
        self.assertEqual(report['status'],'ready')
        self.assertTrue(report['needsMigration'])
        self.assertTrue(all(row['verification']['status']=='not_run' for row in report['items']))

    def test_core_pack_and_disabled_choices_are_independent_of_verification(self):
        self.declare(http=self.http(False, 'pack'), resource_pool={'required': False, 'reason': 'No shared identities'})
        report = self.report()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(self.row(report)['mode'], 'pack')
        self.assertEqual(self.row(report, 'resource_pool')['sources'], ['manifest:resource_pool'])
        self.assertEqual(self.row(report)['verification']['status'], 'not_run')

    def test_required_capability_absent_or_unverified_is_incomplete(self):
        for mode in ('disabled', 'unconfigured', 'unavailable'):
            self.declare(http={'mode': mode, 'required': True, 'reason': 'Pending explicit integration'})
            with self.subTest(mode=mode):
                self.assertEqual(self.report()['status'], 'incomplete')
                self.assertIsNone(self.report()['ok'])
        self.declare(http=self.http())
        self.assertEqual(self.report()['status'], 'incomplete')

    def test_existing_enable_fields_are_the_only_truth(self):
        self.declare(resource_pool={'required': False, 'mode': 'core'})
        with self.assertRaises(ValueError):capabilities.validate_adoption(self.manifest)
        self.declare(diagnostics={'required': False, 'mode': 'core'})
        with self.assertRaises(ValueError):capabilities.validate_adoption(self.manifest)
        self.manifest['diagnostic_capabilities'] = ['database']
        self.declare(diagnostics={'required': False})
        self.assertEqual(self.row(capability='diagnostics')['mode'], 'core')

    def test_nested_manifest_field_sources_are_valid_public_references(self):
        item = {'required': False, 'reason': 'No shared identities',
                'sources': ['manifest:resource_pool.enabled']}
        self.declare(resource_pool=item)
        self.assertIn('manifest:resource_pool.enabled', self.row(capability='resource_pool')['sources'])

    def test_legacy_resource_derivation_matches_reserved_category_rules(self):
        del self.manifest['resource_pool']
        self.manifest['identity_requirements'] = {'scenario_fields': [], 'notes': []}
        self.write_manifest()
        self.assertEqual(self.row(capability='resource_pool')['mode'], 'disabled')
        self.manifest['identity_requirements']['merchant'] = []
        self.write_manifest()
        self.assertEqual(self.row(capability='resource_pool')['mode'], 'core')

    def test_invalid_shape_types_sources_and_secret_value_fields_are_rejected(self):
        cases = [
            None,
            {'version': True, 'items': {}}, {'version': 1, 'items': []},
            {'version': 1, 'items': {'unknown': self.http()}},
            {'version': 1, 'items': {'http': {**self.http(), 'required': 'true'}}},
            {'version': 1, 'items': {'http': {**self.http(), 'required_scope': None}}},
            {'version': 1, 'items': {'http': {**self.http(), 'mode': []}}},
            {'version': 1, 'items': {'http': {**self.http(), 'sources': ['config:TOKEN=private']}}},
            {'version': 1, 'items': {'http': {**self.http(), 'sources': ['adapter:packs/other/integration.py']}}},
            {'version': 1, 'items': {'http': {**self.http(), 'sources': ['adapter:packs/sample/../../secret.py']}}},
            {'version': 1, 'items': {'http': {**self.http(), 'sources': ['adapter:packs/sample/.env']}}},
            {'version': 1, 'items': {'http': {**self.http(), 'token': 'private'}}},
            {'version': 1, 'items': {'http': {**self.http(), 'mode': 'pack', 'reason': ''}}},
        ]
        for declaration in cases:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                capabilities.validate_adoption({**self.manifest, 'capability_adoption': declaration})

    def test_required_synthetic_pass_is_scoped_and_does_not_verify_business(self):
        self.declare(http=self.http())
        _, task_report = self.record()
        report = self.report()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(self.row(report)['verification']['scope'], 'synthetic')
        self.assertFalse(report['realBusinessVerified'])
        self.assertFalse(task_report['realBusinessVerified'])
        self.assertEqual(len(task_report['tasks']), 6)

    def test_manual_review_keeps_its_explicit_scope(self):
        self.declare(http=self.http())
        self.record(self.fact(scope='manual_review'))
        fact = self.row()['verification']
        self.assertEqual(fact['scope'], 'manual_review')
        self.assertEqual(fact['status'], 'passed')
        self.assertFalse(self.report()['realBusinessVerified'])

    def test_required_scope_is_exact_and_review_does_not_satisfy_runtime_expectation(self):
        self.declare(http={**self.http(), 'required_scope': 'synthetic'})
        self.record(self.fact(scope='manual_review'))
        self.assertEqual(self.row()['verification']['status'], 'passed')
        self.assertEqual(self.row()['status'], 'incomplete')
        self.assertEqual(self.report()['status'], 'incomplete')
        self.record(self.fact(scope='mock'))
        self.assertEqual(self.report()['status'], 'incomplete')
        self.record(self.fact(scope='synthetic'))
        self.assertEqual(self.report()['status'], 'ready')
        self.assertEqual(self.row()['requiredScope'], 'synthetic')

    def readonly_tasks(self):
        scope = self.root / 'artifacts/sample/scope.json'
        scope.write_text(json.dumps({'pack': 'sample', 'authorizedReadOnly': True, 'caseIds': ['REAL-R01']}))
        project_tasks.record('sample', 'scope', scope, root=self.root)
        real = self.root / 'artifacts/sample/real.json'
        real.write_text(json.dumps({'pack': 'sample', 'ok': True, 'scope': 'real_readonly',
                                    'realBusinessVerified': True, 'caseIds': ['REAL-R01']}))
        project_tasks.record('sample', 'real_readonly', real, root=self.root)
        return scope, real

    def readonly_fact(self, **extra):
        scope = self.root / 'artifacts/sample/scope.json'
        return self.fact(scope='real_readonly', realBusinessVerified=True, caseIds=['REAL-R01'],
                         scopeEvidenceSha256=hashlib.sha256(scope.read_bytes()).hexdigest(), **extra)

    def test_real_readonly_cannot_bypass_existing_scope_and_real_tasks(self):
        self.declare(http={**self.http(), 'required_scope': 'real_readonly'})
        with self.assertRaises(ValueError):self.record(self.fact(scope='real_readonly'))
        forged = self.fact(scope='real_readonly', realBusinessVerified=True, caseIds=['REAL-R01'],
                           scopeEvidenceSha256='a' * 64)
        with self.assertRaises((ValueError, OSError)):self.record(forged)
        self.readonly_tasks()
        fact = self.readonly_fact()
        for field, value in (('caseIds', ['REAL-R02']), ('scopeEvidenceSha256', 'a' * 64), ('realBusinessVerified', False)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.record({**fact, field: value})
        self.record(fact)
        self.assertEqual(self.report()['status'], 'ready')
        self.assertEqual(self.row()['verification']['scope'], 'real_readonly')
        self.assertEqual(self.row()['verification']['readOnlyAuthorization']['caseIds'], ['REAL-R01'])
        self.assertFalse(self.report()['realBusinessVerified'])

    def test_readonly_scope_or_real_evidence_changes_invalidate_capability(self):
        self.declare(http={**self.http(), 'required_scope': 'real_readonly'})
        scope, real = self.readonly_tasks()
        self.record(self.readonly_fact())
        original = real.read_bytes()
        real.write_text('{}')
        self.assertEqual(self.row()['verification']['status'], 'stale')
        real.write_bytes(original)
        self.assertEqual(self.row()['verification']['status'], 'passed')
        scope.unlink()
        self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_optional_actual_failure_is_failed_and_incomplete_is_not_false_success(self):
        self.declare(http=self.http(False))
        self.record(self.fact(status='failed'))
        self.assertEqual(self.report()['status'], 'failed')
        self.assertIs(self.report()['ok'], False)
        self.declare(http=self.http())
        self.record(self.fact(status='incomplete'))
        self.assertEqual(self.report()['status'], 'incomplete')
        self.assertIsNone(self.row()['verification']['ok'])

    def test_fact_requires_raw_types_consistent_status_and_scope(self):
        self.declare(http=self.http())
        for changes in ({'verifiedAt': True}, {'scope': 'real_write'}, {'scope': []}, {'ok': 'true'},
                        {'status': 'incomplete', 'ok': False}, {'realBusinessVerified': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.record(self.fact(**changes))

        report = self.fact(status='incomplete')
        del report['ok']
        with self.assertRaises(ValueError):self.record(report)

    def test_report_changes_or_deletion_are_stale(self):
        self.declare(http=self.http())
        path, _ = self.record()
        original = path.read_bytes()
        path.write_text('{}')
        self.assertEqual(self.row()['verification']['status'], 'stale')
        path.write_bytes(original)
        self.assertEqual(self.row()['verification']['status'], 'passed')
        path.unlink()
        self.assertEqual(self.row()['verification']['status'], 'stale')
        self.assertEqual(self.report()['status'], 'incomplete')

    def test_reused_targeted_report_change_or_deletion_invalidates_fact(self):
        self.declare(http=self.http())
        source = self.root / 'artifacts/sample/targeted.json'
        source.write_text('{"ok":true,"groups":["sample.adapter"]}')
        reference = {'path': 'artifacts/sample/targeted.json',
                     'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        self.record(self.fact(evidenceRefs=[reference]))
        self.assertEqual(self.row()['verification']['evidenceRefs'], [reference])
        original = source.read_bytes()
        source.write_text('{}')
        self.assertEqual(self.row()['verification']['status'], 'stale')
        source.write_bytes(original)
        self.assertEqual(self.row()['verification']['status'], 'passed')
        source.unlink()
        self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_reused_report_cannot_reference_other_pack_or_mismatching_digest(self):
        self.declare(http=self.http())
        for reference in ({'path': 'artifacts/other/test.json', 'sha256': 'a' * 64},
                          {'path': 'artifacts/sample/http.json', 'sha256': 'a' * 64}):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.record(self.fact(evidenceRefs=[reference]))

    def test_declaration_changes_invalidate_evidence_without_modifying_report(self):
        self.declare(http=self.http())
        self.record()
        self.manifest['capability_adoption']['items']['http']['reason'] = 'Policy changed'
        self.write_manifest()
        self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_platform_version_change_invalidates_evidence(self):
        self.declare(http=self.http())
        self.record()
        with patch.object(capabilities.platform_protocol, 'API_VERSION', '1.9'):
            self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_same_version_core_assets_and_inventory_changes_invalidate_evidence(self):
        self.declare(http=self.http())
        for relative in ('core_sample.py', 'assets/shared.js', 'resources/platform-core-files.json'):
            self.record()
            path = self.root / relative
            original = path.read_bytes()
            if path.suffix == '.json':path.write_text('["core_sample.py"]\n')
            else:path.write_bytes(original + b'\n# changed\n')
            with self.subTest(relative=relative):
                self.assertEqual(self.row()['verification']['status'], 'stale')
            path.write_bytes(original)

    def test_core_inventory_cannot_open_pack_data_or_external_symlinks(self):
        self.declare(http=self.http())
        inventory = self.root / 'resources/platform-core-files.json'
        inventory.write_text('["packs/other/integration.py"]')
        with self.assertRaises(ValueError):self.report()
        inventory.write_text('["core_sample.py"]')
        source = self.root / 'core_sample.py'
        source.unlink();source.symlink_to(self.root / 'packs/other/integration.py')
        with self.assertRaises(ValueError):self.report()

    def test_panel_entry_source_and_selection_changes_invalidate_evidence(self):
        self.manifest['panel_html'] = 'packs/sample/panel.html'
        panel = self.root / self.manifest['panel_html']
        panel.write_text('<main>synthetic</main>')
        self.declare(http=self.http())
        self.record()
        panel.write_text('<main>changed</main>')
        self.assertEqual(self.row()['verification']['status'], 'stale')
        self.record()
        self.manifest['panel_html'] = 'packs/sample/second.html'
        (self.root / self.manifest['panel_html']).write_text('<main>another</main>')
        self.write_manifest()
        self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_adapter_edit_delete_and_bootstrap_change_invalidate_evidence(self):
        self.declare(http=self.http())
        source = self.root / 'packs/sample/integration.py'
        for change in ('edit', 'delete', 'bootstrap'):
            source.write_text('# initial\n')
            self.record()
            if change == 'edit':source.write_text('# changed\n')
            elif change == 'delete':source.unlink()
            else:(self.root / 'packs/sample/__init__.py').write_text('# changed bootstrap\n')
            with self.subTest(change=change):
                self.assertEqual(self.row()['verification']['status'], 'stale')

    def test_old_binding_cannot_be_relabelled_current_at_record_time(self):
        self.declare(http=self.http())
        old = self.fact()
        (self.root / 'packs/sample/integration.py').write_text('# new code\n')
        with self.assertRaises(ValueError):self.record(old)

    def test_missing_adapter_source_cannot_gain_pass_receipt(self):
        self.declare(http=self.http())
        (self.root / 'packs/sample/integration.py').unlink()
        with self.assertRaises(ValueError):self.record()

    def test_cross_pack_report_and_symlink_sources_never_follow_another_pack(self):
        self.declare(http=self.http())
        other = self.root / 'artifacts/other/evidence.json'
        other.write_text(json.dumps(self.fact()))
        with self.assertRaises(ValueError):
            project_tasks.record_capability('sample', 'http', other, root=self.root)
        path = self.root / 'packs/sample/integration.py'
        path.unlink();path.symlink_to(self.root / 'packs/other/integration.py')
        with self.assertRaises(ValueError):self.report()

    def test_doctor_and_tasks_reject_cross_pack_manifest_before_reading_it(self):
        other = self.root / 'packs/other/pack.json'
        other.write_text('{"id":"other"}')
        path = self.root / 'packs/sample/pack.json'
        path.unlink();path.symlink_to(other)
        original = Path.read_bytes
        def guarded_read(path):
            if path.resolve() == other:raise AssertionError('Another Pack was read')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded_read):
            self.assertEqual(project_doctor.diagnose('sample', root=self.root)['status'], 'failed')
            with self.assertRaises(ValueError):project_tasks.capability_context('sample', 'http', root=self.root)

    def test_symlinked_task_or_evidence_directories_cannot_borrow_other_pack(self):
        self.declare(http=self.http())
        self.record()
        sample = self.root / 'artifacts/sample'
        (sample / 'http.json').unlink();sample.rmdir()
        sample.symlink_to(self.root / 'artifacts/other', target_is_directory=True)
        with self.assertRaises(ValueError):self.report()

    def test_evidence_body_config_values_and_private_fields_are_not_projected(self):
        self.declare(http=self.http())
        self.record(self.fact(token='synthetic-secret', body={'password': 'synthetic-password'}))
        value = json.dumps(self.report())
        for private in ('synthetic-secret', 'synthetic-password', 'do-not-project-this-value'):
            self.assertNotIn(private, value)
        self.assertIn('config:SERVICE_URL', value)
        self.assertNotIn(str(self.root), value)

    def test_registered_path_objects_do_not_change_the_portable_projection(self):
        self.declare(http=self.http())
        prepared = {**self.manifest, 'root': self.root, 'case_dirs': [self.root / 'packs/sample/cases']}
        self.assertEqual(capabilities.adoption_report(prepared, root=self.root), self.report())

    def test_doctor_tasks_and_direct_projection_agree_without_pack_import_or_business_probe(self):
        self.declare(http=self.http())
        self.record()
        with patch.object(project_doctor, 'verify', return_value={'cases': 1}), \
                patch('socket.socket', side_effect=AssertionError('No probe authorized')):
            doctor = project_doctor.diagnose('sample', root=self.root)
            tasks = project_tasks.status('sample', root=self.root)
        self.assertEqual(doctor['capabilityAdoption'], self.report())
        self.assertEqual(tasks['capabilityAdoption'], self.report())
        self.assertFalse(doctor['realBusinessVerified'])
        saved = json.loads((self.root / 'data/sample/onboarding-tasks.json').read_text())
        self.assertNotIn('capabilityAdoption', saved)
        self.assertEqual(len(saved['tasks']), 6)

    def test_doctor_marks_conflicting_declaration_failed_and_missing_required_evidence_incomplete(self):
        self.declare(http=self.http())
        with patch.object(project_doctor, 'verify', return_value={'cases': 1}):
            doctor = project_doctor.diagnose('sample', root=self.root)
            self.assertEqual(doctor['status'], 'incomplete')
            self.assertIsNone(doctor['ok'])
            self.declare(resource_pool={'mode': 'core', 'required': False})
            self.assertEqual(project_doctor.diagnose('sample', root=self.root)['status'], 'failed')

    def test_corrupt_receipt_cannot_claim_pass(self):
        self.declare(http=self.http())
        self.record()
        path = self.root / 'data/sample/onboarding-tasks.json'
        state = json.loads(path.read_text())
        state['capabilityEvidence']['http']['private'] = 'not-projected'
        path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):self.report()


if __name__ == '__main__':
    unittest.main()
