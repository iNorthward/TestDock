from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apifox_contract_audit as contract_audit  # noqa: E402
import run_apifox_inspection as inspection  # noqa: E402


class ApifoxOasAuditInputTests(unittest.TestCase):
    def test_load_ops_ignores_path_item_fields_and_invalid_methods(self):
        oas = {"paths": {"/mgr/demo": {
            "summary": "path summary",
            "description": "path description",
            "parameters": [{"name": "tenant", "in": "header"}],
            "$ref": "#/components/pathItems/Demo",
            "x-apifox-folder": "Demo",
            "get": {"summary": "read"},
            "POST": {"summary": "write"},
            "trace": {"summary": "unsupported"},
            "put": "not-an-operation",
        }}}

        operations = contract_audit.load_ops_from_oas(oas)

        self.assertEqual(set(operations), {("GET", "/mgr/demo"), ("POST", "/mgr/demo")})
        self.assertEqual(operations[("GET", "/mgr/demo")]["summary"], "read")

    def test_skip_fetch_rejects_empty_index_before_inspection_steps(self):
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "apifox-index.json"
            index.write_text(json.dumps({"paths": {}}), encoding="utf-8")
            with patch.object(inspection, "APIFOX_INDEX", index):
                with self.assertRaisesRegex(RuntimeError, "跳过拉取时 Apifox OAS 索引不可用"):
                    inspection.step_a_fetch(skip=True)

    def test_step_b_rejects_empty_local_or_online_snapshot(self):
        valid = {"paths": {"/mgr/demo": {"get": {}}}}
        empty = {"paths": {}}
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "local.json"
            online = Path(td) / "online.json"
            with patch.object(inspection, "LOCAL_OAS", local), \
                    patch.object(inspection, "APIFOX_INDEX", online):
                local.write_text(json.dumps(empty), encoding="utf-8")
                online.write_text(json.dumps(valid), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "没有有效 HTTP operation"):
                    inspection.step_b_audit()

                local.write_text(json.dumps(valid), encoding="utf-8")
                online.write_text(json.dumps(empty), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "没有有效 HTTP operation"):
                    inspection.step_b_audit()

    def test_contract_audit_cli_fails_closed_on_empty_snapshot(self):
        valid = {"paths": {"/mgr/demo": {"get": {}}}}
        empty = {"paths": {}}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local.json"
            online = root / "online.json"
            for bad_side in ("local", "online"):
                with self.subTest(bad_side=bad_side):
                    local.write_text(json.dumps(empty if bad_side == "local" else valid), encoding="utf-8")
                    online.write_text(json.dumps(empty if bad_side == "online" else valid), encoding="utf-8")
                    stdout = io.StringIO()
                    with patch.object(contract_audit, "LOCAL_OAS", local), \
                            patch.object(sys, "argv", [
                                "apifox_contract_audit.py", "--apifox-index", str(online), "--json",
                            ]), \
                            patch.object(contract_audit, "covered_ops", side_effect=AssertionError("audit must not start")), \
                            contextlib.redirect_stdout(stdout), \
                            self.assertRaises(SystemExit) as raised:
                        contract_audit.main()

                    self.assertEqual(raised.exception.code, 1)
                    self.assertFalse(json.loads(stdout.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
