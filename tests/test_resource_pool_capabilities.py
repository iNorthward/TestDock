"""Project taxonomy, optional resources and cross-project isolation contracts."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'test-platform')]
import identity_pool as pool
from resource_pool_config import resource_pool_definition
from identity_pool_audit_core import build_audit, plan_safe_changes


class ResourceCapabilitiesTests(unittest.TestCase):
    def custom(self):
        return {'id': 'iot', 'resource_pool': {'enabled': True, 'title': '设备资源', 'categories': [
            {'id': 'device', 'label': '测试设备', 'display_fields': ['serial_number', 'token']},
            {'id': 'service_account', 'label': '服务账号'}]},
            'identity_requirements': {'device': ['probe']}}

    def test_pool_disabled_does_not_load_storage_or_audit(self):
        with patch('identity_pool.selected_definition', return_value=resource_pool_definition({'resource_pool': {'enabled': False}})), \
                patch('identity_pool.load_pool') as loader:
            result = pool.panel_snapshot()
        self.assertFalse(result['enabled'])
        self.assertEqual(result['categories'], {})
        loader.assert_not_called()

    def test_arbitrary_project_categories_are_audited_and_redacted(self):
        pack = self.custom()
        data = pool.empty_pool()
        data['identities'] = [{'id': 'one', 'category': 'device', 'role': 'probe',
                               'serial_number': 'SYNTHETIC', 'token': 'DO_NOT_EXPOSE'},
                              {'id': 'two', 'category': 'service_account', 'role': 'reader'}]
        pool.validate_pool(data)
        with patch('pack_registry.load_packs', return_value=[pack]), patch('identity_pool.load_pool', return_value=data):
            result = pool.panel_snapshot()
        self.assertEqual(set(result['categories']), {'device', 'service_account'})
        item = result['categories']['device']
        self.assertEqual(item['label'], '测试设备')
        self.assertEqual(item['identities'][0]['serial_number'], 'SYNTHETIC')
        self.assertNotIn('DO_NOT_EXPOSE', str(result))
        self.assertEqual(item['missing'], [])
        data['identities'] = []
        report = build_audit(pack_id='iot', pack=pack, pool=data, code_scan={}, pool_file='temporary')
        self.assertEqual([(r['category'], r['role']) for r in report['missing_roles']], [('device', 'probe')])

    def test_scenes_and_fixtures_are_only_exposed_when_declared(self):
        pack = self.custom()
        pack['resource_pool']['categories'] = [{'id': 'sample', 'label': '样本', 'storage': 'scenarios'}]
        with patch('pack_registry.load_packs', return_value=[pack]), patch('identity_pool.load_pool', return_value=pool.empty_pool()):
            result = pool.panel_snapshot()
        self.assertEqual(set(result['categories']), {'sample'})
        self.assertEqual(result['categories']['sample']['storage'], 'scenarios')

    def test_invalid_declarations_are_rejected_before_bootstrap(self):
        for raw in ({'enabled': 'false'}, {'enabled': True, 'categories': [{'id': '../other'}]},
                    {'enabled': False, 'categories': [{'id': 'device'}]},
                    {'enabled': True, 'categories': [{'id': 'device'}, {'id': 'device'}]},
                    {'enabled': True, 'categories': [{'id': 'device', 'display_fields': 'token'}]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                resource_pool_definition({'resource_pool': raw})

    def test_no_implicit_business_categories_or_default_aliases(self):
        self.assertFalse(resource_pool_definition({})['enabled'])
        data = pool.empty_pool()
        data['identities'] = [{'id': 'one', 'category': 'user', 'role': 'default_client_login',
                               'username': 'synthetic', 'password': 'synthetic'}]
        plan = plan_safe_changes(pack={}, pool=data, audit={'missing_roles': [{'category': 'user', 'role': 'extra'}]},
                                 code_scan={'roles': {('user', 'extra'): {'source_kinds': ['cli_user']}}})
        self.assertEqual(plan['append_roles'], [])

    def test_fixture_classification_and_values_are_not_guessed(self):
        from identity_pool_audit_core import is_negative_fixture
        self.assertFalse(is_negative_fixture('anything.nonexistent'))
        self.assertTrue(is_negative_fixture('anything.nonexistent', {'notes': {'fixture_name_pattern': 'nonexistent'}}))
        requirements = {'notes': {'fixture_name_pattern': 'invalid'}, 'scenario_fields': {'invalid_sample': ['amount']}}
        result = plan_safe_changes(pack={'id': 'iot', 'identity_requirements': requirements}, pool=pool.empty_pool(),
                                   audit={'missing_fixtures': [{'name': 'invalid_sample'}]}, code_scan={})
        self.assertEqual(result['add_fixtures'], [])

    def test_extension_fields_do_not_inherit_business_id_shapes(self):
        data=pool.empty_pool()
        data['identities']=[{'id':'device-one','category':'device','role':'probe',
                             'uid':{'vendor':'synthetic','value':[1,2]},'parent_id':False}]
        pool.validate_pool(data)
        self.assertEqual(data['identities'][0]['uid']['value'],[1,2])

    def test_optional_diagnostics_are_explicit(self):
        from pack_registry import diagnostic_capabilities
        self.assertEqual(diagnostic_capabilities({}),[])
        self.assertEqual(diagnostic_capabilities({'diagnostic_capabilities':['database']}),['database'])
        for values in (['database','database'],['foreign'],True):
            with self.assertRaises(ValueError):diagnostic_capabilities({'diagnostic_capabilities':values})
