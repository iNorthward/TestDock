"""Core validators accept only explicit project extensions."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test-platform/common'))
import json_wire_types as wire


class WireRuleRegistryTests(unittest.TestCase):
    def test_unknown_policy_is_incomplete_even_when_field_is_absent(self):
        models = {'Row': {'fields': {'id': {'wire': 'example_policy'}}, 'refs': {}}}
        with patch.object(wire, '_PROJECT_WIRE_RULES', {}), patch.object(wire, '_wire_manifest', return_value=models):
            with self.assertRaisesRegex(ValueError, '未注册 wire 规则'):
                wire.schema_wire_errors({}, 'Row')

    def test_custom_policy_preserves_raw_types_and_can_be_unregistered(self):
        models = {'Row': {'fields': {'id': {'wire': 'example_policy'}}, 'refs': {}}}
        with patch.object(wire, '_PROJECT_WIRE_RULES', {}), patch.object(wire, '_wire_manifest', return_value=models):
            validator = lambda value: value if type(value) is str and value == '42' else None
            self.assertIsNone(wire.register_wire_rule('example_policy', validator))
            self.assertEqual(wire.schema_wire_errors({'id': '42'}, 'Row'), [])
            for value in (42, True, '0042'):
                self.assertTrue(wire.schema_wire_errors({'id': value}, 'Row'))
            self.assertIs(wire.register_wire_rule('example_policy', None), validator)
            with self.assertRaises(ValueError):
                wire.schema_wire_errors({'id': '42'}, 'Row')

    def test_builtin_contracts_cannot_be_replaced(self):
        for name in ('int64_string', 'json_number', 'json_integer', 'decimal_string', 'decimal_text'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.register_wire_rule(name, lambda value: value)
