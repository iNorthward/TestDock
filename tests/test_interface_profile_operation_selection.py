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

import gen_interface_profile as profile  # noqa: E402
import atomic_file  # noqa: E402


class InterfaceProfileOperationSelectionTests(unittest.TestCase):
    def test_default_selection_skips_valid_path_item_metadata(self):
        path_item = {
            "summary": "path summary",
            "description": "path description",
            "parameters": [{"name": "tenant", "in": "header"}],
            "$ref": "#/components/pathItems/Demo",
            "x-apifox-folder": "Demo",
            "get": {"summary": "read"},
            "trace": {"summary": "unsupported"},
            "put": "invalid operation",
        }

        method, operation = profile.select_operation(path_item)

        self.assertEqual(method, "get")
        self.assertEqual(operation["summary"], "read")

    def test_explicit_method_is_case_insensitive_and_must_be_supported(self):
        method, operation = profile.select_operation({"post": {"summary": "write"}}, "POST")

        self.assertEqual(method, "post")
        self.assertEqual(operation["summary"], "write")
        with self.assertRaisesRegex(ValueError, "不支持的 HTTP 方法"):
            profile.select_operation({"trace": {"summary": "unsupported"}}, "TRACE")

    def test_path_item_without_valid_operations_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "没有有效 HTTP operation"):
            profile.select_operation({"summary": "metadata only", "get": "invalid"})
        with self.assertRaisesRegex(ValueError, "Path Item 必须是对象"):
            profile.select_operation(None)

    def test_force_replace_failure_preserves_existing_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oas_path = root / "oas.json"
            oas_path.write_text(json.dumps({
                "paths": {
                    "/mgr/demo": {
                        "summary": "Path Item metadata precedes method",
                        "get": {"summary": "read"},
                    }
                }
            }), encoding="utf-8")
            profile_dir = root / "profiles"
            profile_dir.mkdir()
            existing = profile_dir / "get_mgr_demo.md"
            existing.write_text("human-confirmed content", encoding="utf-8")
            argv = ["gen_interface_profile.py", "--path", "/mgr/demo", "--force"]

            with patch.object(profile, "LOCAL_OAS", oas_path), \
                    patch.object(profile, "SCHEMA_JSON", root / "missing-schema.json"), \
                    patch.object(profile, "COVERAGE_JSON", root / "missing-coverage.json"), \
                    patch.object(profile, "OUT_DIR", profile_dir), \
                    patch.object(sys, "argv", argv), \
                    patch.object(atomic_file.os, "replace", side_effect=OSError("replace failed")), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(OSError, "replace failed"):
                profile.main()

            self.assertEqual(existing.read_text(encoding="utf-8"), "human-confirmed content")
            self.assertEqual(list(profile_dir.glob(".get_mgr_demo.md.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
