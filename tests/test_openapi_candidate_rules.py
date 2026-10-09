from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from openapi_contract import OpenAPIContract, resolve_schema, schema_issues
from candidate_inputs import iter_openapi_operations
import gen_r_series_candidates as r
import gen_e_series_candidates as e
import gen_f_series_candidates as f
import gen_d_series_candidates as d
import coverage_scorecard as score
import check_e_coverage_gap as gap


def response(data, status='200'):
    return {'responses': {status: {'content': {'application/json': {'schema': data}}}}}


def envelope(data):
    return response({'type': 'object', 'properties': {
        'success': {'type': 'boolean'}, 'code': {'type': 'integer'},
        'msg': {'type': 'string'}, 'data': data,
    }})


def candidate(result, kind):
    return next(c for c in result['candidates'] if c['kind'] == kind)


class OpenAPICandidateRuleTests(unittest.TestCase):
    def test_parameter_refs_inheritance_and_operation_override(self):
        doc = {'components': {'parameters': {'ID': {'name': 'id', 'in': 'query', 'required': True,
               'schema': {'type': 'integer', 'format': 'int64'}}}}, 'paths': {'/demo/page': {
               'parameters': [{'$ref': '#/components/parameters/ID'}, {'name': 'q', 'in': 'query', 'required': True}],
               'get': {'parameters': [{'name': 'q', 'in': 'query', 'required': False}]}}}}
        _, _, spec = next(iter_openapi_operations(doc))
        self.assertEqual([p['name'] for p in spec['parameters']], ['id', 'q'])
        result = e.derive_for_op('GET', '/demo/page', spec, {})
        self.assertEqual([c['param'] for c in result['candidates'] if c['kind'] == 'missing_required'], ['id'])
        self.assertIn('int64', candidate(result, 'number_boundary')['desc'])

    def test_same_named_schema_is_isolated_between_documents(self):
        for wire_type in ('string', 'integer', 'string'):
            doc = {'components': {'schemas': {'Row': {'type': 'object', 'required': ['id'],
                   'properties': {'id': {'type': wire_type}}}}},
                   'paths': {'/detail': {'get': envelope({'$ref': '#/components/schemas/Row'})}}}
            _, path, spec = next(iter_openapi_operations(doc))
            fields = candidate(r.derive_for_op('GET', path, spec, {}), 'field_contract')['fields']
            self.assertEqual(fields[0]['type'], wire_type)
            self.assertTrue(fields[0]['required'])

    def test_allof_preserves_field_and_object_constraints(self):
        schema = resolve_schema({'allOf': [{'type': 'object', 'required': ['id'], 'properties': {
            'id': {'type': 'string', 'nullable': True, 'format': 'uuid'}}},
            {'properties': {'id': {'maxLength': 40}, 'status': {'type': 'integer', 'enum': [0, 1]}},
             'required': ['status']}], 'additionalProperties': False})
        self.assertEqual(schema['required'], ['id', 'status'])
        self.assertFalse(schema['additionalProperties'])
        fields = candidate(r.derive_for_op('GET', '/detail', envelope(schema), {}), 'field_contract')['fields']
        self.assertTrue(fields[0]['nullable'])
        self.assertEqual(fields[0]['schema']['maxLength'], 40)
        self.assertEqual(fields[1]['enum'], [0, 1])
        self.assertNotIn('必须存在且非空', candidate(r.derive_for_op('GET', '/detail', envelope(schema), {}), 'field_contract')['expect'])

    def test_scalar_allof_does_not_become_object(self):
        schema = resolve_schema({'allOf': [{'type': 'integer', 'format': 'int64'}, {'minimum': 0}]})
        self.assertEqual(schema, {'type': 'integer', 'format': 'int64', 'minimum': 0})

    def test_unsafe_refs_and_composition_are_explicit(self):
        doc = {'components': {'schemas': {'Loop': {'$ref': '#/components/schemas/Loop'}}}}
        for raw in ({'$ref': '#/components/schemas/Loop'}, {'$ref': '#/missing'},
                    {'$ref': 'external.json#/Row'}, {'oneOf': []},
                    {'allOf': [{'type': 'string'}, {'type': 'integer'}]}):
            with self.subTest(raw=raw):
                schema = resolve_schema(raw, doc)
                self.assertTrue(schema_issues(schema))
                result = r.derive_for_op('GET', '/detail', response(schema), {})
                self.assertTrue(candidate(result, 'response_schema_unresolved')['uncertain'])

    def test_json_pointer_escape(self):
        doc = {'components': {'schemas': {'a/b~c': {'type': 'string'}}}}
        self.assertEqual(resolve_schema({'$ref': '#/components/schemas/a~1b~0c'}, doc)['type'], 'string')

    def test_missing_parameter_ref_rejects_instead_of_dropping_it(self):
        doc = {'paths': {'/demo': {'get': {'parameters': [{'$ref': '#/missing'}]}}}}
        with self.assertRaisesRegex(ValueError, '无法解析 parameter'):
            list(iter_openapi_operations(doc))

    def test_pagination_wire_types_and_named_business_fields_preserved(self):
        result = r.derive_for_op('GET', '/page', envelope({'type': 'object', 'properties': {
            'total': {'type': 'string'}, 'records': {'type': 'array', 'items': {'type': 'object',
            'properties': {'total': {'type': 'string'}, 'current': {'type': 'integer'}, 'orders': {'type': 'array'}}}}}}), {})
        self.assertEqual(candidate(result, 'page_envelope')['fields'][0]['type'], 'string')
        self.assertEqual([f['name'] for f in candidate(result, 'field_contract')['fields']], ['total', 'current', 'orders'])
        self.assertNotIn('code=200', candidate(result, 'contract_base')['expect'])
        self.assertNotIn('回显请求分页', candidate(result, 'page_envelope')['expect'])

    def test_records_name_without_array_is_not_a_page(self):
        self.assertEqual(r.walk_response(envelope({'properties': {'records': {'type': 'string'}}}))['kind'], 'unresolved')

    def test_success_statuses_and_no_content(self):
        self.assertEqual(r.walk_response(response({'type': 'string'}, '201'))['kind'], 'raw_scalar')
        result = r.derive_for_op('GET', '/empty', {'responses': {'204': {'description': 'empty'}}}, {})
        self.assertEqual(candidate(result, 'no_content_response')['expect'], 'HTTP 204；响应体为空')
        for spec in ({'responses': {}}, {'responses': {'200': {'description': 'unknown'}}},
                     {'responses': {'200': {}, '204': {}}}):
            self.assertEqual(r.walk_response(spec)['kind'], 'unresolved')

    def test_component_response_and_body_refs(self):
        doc = {'components': {'requestBodies': {'Body': {'content': {'application/json': {'schema': {
            'type': 'object', 'required': ['amount'], 'properties': {'amount': {'type': 'number'}}}}}}},
            'responses': {'Read': {'content': {'application/json': {'schema': {'type': 'string'}}}}}},
            'paths': {'/demo': {'get': {'responses': {'200': {'$ref': '#/components/responses/Read'}}},
                               'patch': {'requestBody': {'$ref': '#/components/requestBodies/Body'}}}}}
        ops = list(iter_openapi_operations(doc))
        self.assertEqual(r.walk_response(ops[0][2])['kind'], 'raw_scalar')
        patch_result = e.derive_for_op(*ops[1], {})
        self.assertEqual(candidate(patch_result, 'body_missing_required')['param'], 'amount')
        self.assertNotIn('method_not_allowed', [c['kind'] for c in patch_result['candidates']])

    def test_unresolved_body_is_not_invented_required_field(self):
        spec = {'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/missing'}}}}}
        for method in ('POST', 'PATCH', 'DELETE'):
            result = e.derive_for_op(method, '/save', spec, {})
            self.assertIn('request_schema_unresolved', [c['kind'] for c in result['candidates']])
            self.assertNotIn('body_missing_required', [c['kind'] for c in result['candidates']])

    def test_explicit_security_overrides_pack_path_heuristic(self):
        result = e.derive_for_op('GET', '/mgr/public/read', {'security': [{'key': []}]}, {})
        self.assertFalse(result['public'])
        for security in ([], [{}, {'key': []}]):
            result = e.derive_for_op('GET', '/protected/name', {'security': security}, {})
            self.assertTrue(result['public'])
            self.assertEqual([c['kind'] for c in result['candidates']], ['public_no_auth'])

    def test_root_security_inheritance_and_override(self):
        doc = {'security': [{'token': []}], 'paths': {'/a': {'get': {}}, '/b': {'get': {'security': []}}}}
        specs = {path: spec for _, path, spec in iter_openapi_operations(doc)}
        self.assertEqual(specs['/a']['security'], [{'token': []}])
        self.assertEqual(specs['/b']['security'], [])

    def test_unknown_role_and_prefix_collision_stay_unconfirmed(self):
        self.assertEqual(e.side_of('/manager/read')[0], 'other')
        result = e.derive_for_op('GET', '/unknown/read', {}, {})
        self.assertTrue(all(c.get('uncertain') for c in result['candidates']))
        with patch.object(e, 'PUBLIC_PATH_SEGMENTS', set()):
            self.assertFalse(e.is_public('/unknown/public/read'))

    def test_no_int32_overflow_invented_for_number_or_unformatted_integer(self):
        for schema in ({'type': 'number'}, {'type': 'integer'}):
            result = e.derive_for_op('GET', '/read', {'parameters': [{'in': 'query', 'name': 'amount', 'schema': schema}]}, {})
            self.assertNotIn('number_boundary', [c['kind'] for c in result['candidates']])
        result = e.derive_for_op('GET', '/read', {'parameters': [{'in': 'query', 'name': 'amount',
            'schema': {'type': 'number', 'minimum': 0, 'maximum': 10}}]}, {})
        self.assertEqual(candidate(result, 'number_boundary')['constraints'], {'minimum': 0, 'maximum': 10})

    def test_all_cli_consumers_skip_metadata_and_use_current_document(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'oas.json'
            doc = {'components': {'parameters': {'Q': {'name': 'id', 'in': 'query', 'required': True,
                   'schema': {'type': 'integer'}}}}, 'paths': {'x-meta': {'get': {}}, '/demo/page': {
                   'summary': 'metadata', 'x-meta': {}, 'servers': {},
                   'parameters': [{'$ref': '#/components/parameters/Q'}],
                   'get': envelope({'type': 'string'})}, '/invalid': None}}
            path.write_text(json.dumps(doc))
            for module in (r, e, f, d):
                with self.subTest(module=module.__name__), patch.object(module, 'LOCAL_OAS', path), \
                        patch.object(sys, 'argv', [module.__name__, '--json']), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    module.main()
                    results = json.loads(output.getvalue())
                    self.assertEqual([row['endpoint'] for row in results], ['GET /demo/page'])
            with patch.object(e, 'LOCAL_OAS', path):
                self.assertEqual([op[:2] for op in score._iter_ops('', False, {})], [('GET', '/demo/page')])
                self.assertEqual({v['endpoint'] for v in gap.load_candidates().values()}, {'GET /demo/page'})

    def test_nullable_union_keeps_complete_scalar_schema(self):
        schema = {'type': ['string', 'null'], 'maxLength': 20}
        raw = candidate(r.derive_for_op('GET', '/read', response(schema), {}), 'raw_response_type')
        self.assertEqual(raw['scalarType'], 'string')
        self.assertEqual(raw['schema'], schema)
        wrapped = candidate(r.derive_for_op('GET', '/read', envelope(schema), {}), 'data_type')
        self.assertEqual(wrapped['schema'], schema)

    def test_demo_pack_does_not_inherit_sample_project_public_or_role_policy(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'oas.json'
            path.write_text(json.dumps({'paths': {'/cli/public/read': {'get': response({'type': 'string'})}}}))
            env = dict(os.environ, PLATFORM_PACKS='demo_pack', PLATFORM_OAS_BASELINE=str(path), PLATFORM_PACK_DATA_DIR=td)
            for series in ('e', 'r'):
                run = subprocess.run([sys.executable, str(ROOT / 'scripts' / f'gen_{series}_series_candidates.py'),
                    '--prefix', '/cli/public', '--json'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                row = json.loads(run.stdout)[0]
                if series == 'e':
                    self.assertFalse(row['public'])
                    self.assertEqual(row['side'], 'other')
                    self.assertTrue(all(c.get('uncertain') for c in row['candidates']))
                else:
                    self.assertEqual(row['respKind'], 'raw_scalar')
                    self.assertNotIn('contract_base', [c['kind'] for c in row['candidates']])

    def test_f_and_d_business_guesses_are_marked_for_confirmation(self):
        spec = {'parameters': [{'name': 'status', 'in': 'query', 'schema': {'type': 'integer', 'enum': [0, 1]}}]}
        for result in (f.derive_for_op('GET', '/demo/page', spec, {}), d.derive_for_op('GET', '/demo/page', spec, {}, {})):
            self.assertTrue(all(c['uncertain'] for c in result['candidates']))


if __name__ == '__main__':
    unittest.main()
