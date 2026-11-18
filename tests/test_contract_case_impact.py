"""Selected Pack contract previews describe coverage gaps without executing runners."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from contract_case_impact import build_report  # noqa: E402
from validate_catalog import case_apis, case_contract_bindings, check_structure  # noqa: E402
import run_history  # noqa: E402


class ContractCaseImpactTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def snapshot(self, filename, paths, **document):
        path = self.root / filename
        path.write_text(json.dumps(dict(document, paths=paths)), encoding="utf-8")
        return path

    def preview(self, cases, snapshots, *, history=None, runner_api=None):
        def fail_if_called(*_args, **_kwargs):
            self.fail("impact preview must not call the case runner")

        module = SimpleNamespace(CATALOG=cases, RUNNER_API=runner_api or {}, exec_case=fail_if_called)
        return build_report("sample", [module], snapshots, history=history)

    def test_shared_removed_fixture_only_marks_cases_that_declare_it(self):
        paths = {"/items": {"get": {}}, "/labels": {"get": {}}, "/fixtures": {"post": {}}}
        before = self.snapshot("before.json", paths)
        after = self.snapshot("after.json", {"/items": {"get": {}}, "/labels": {"get": {}},
                                             "/unrelated": {"get": {}}})
        cases = [
            {"id": "FIRST", "api": "GET /items", "fixtureApis": ["POST /fixtures"]},
            {"id": "SECOND", "api": ["GET /labels"], "fixtureApis": ["POST /fixtures"]},
            {"id": "LEGACY", "expect": "items"},
            {"id": "NO-FIXTURE", "api": "GET /labels", "fixtureApis": []},
        ]
        report = self.preview(cases, {"inventory": (before, after)}, runner_api={"items": "GET /items"})
        self.assertEqual(report["analysisStatus"], "complete")
        self.assertEqual(report["businessCasesExecuted"], 0)
        self.assertEqual(report["affectedCaseIds"], ["FIRST", "SECOND"])
        by_id = {case["caseId"]: case for case in report["cases"]}
        expected = {"role": "fixture", "source": "inventory", "method": "POST",
                    "path": "/fixtures", "change": "removed"}
        for case_id in report["affectedCaseIds"]:
            self.assertEqual(by_id[case_id]["retestReasons"], [expected])
            self.assertEqual(by_id[case_id]["verification"], {"ok": None, "status": "incomplete"})
        self.assertEqual(by_id["LEGACY"]["associations"]["fixture"]["status"], "undeclared")
        self.assertEqual(by_id["NO-FIXTURE"]["associations"]["fixture"]["status"], "declared")
        self.assertEqual(report["sources"][0]["changes"], [
            {"method": "GET", "path": "/unrelated", "change": "added"},
            {"method": "POST", "path": "/fixtures", "change": "removed"},
        ])

    def test_same_endpoint_in_two_sources_needs_a_scope(self):
        route = {"/shared/{id}": {"get": {}}}
        before = self.snapshot("before.json", route)
        removed = self.snapshot("removed.json", {})
        unchanged = self.snapshot("unchanged.json", route)
        cases = [
            {"id": "AMBIGUOUS", "api": "GET /shared/{id}"},
            {"id": "INVENTORY", "api": [{"source": "inventory", "api": "GET /shared/{id}"}]},
            {"id": "RECORDS", "api": [{"source": "records", "api": "GET /shared/{id}"}]},
            {"id": "EXACT-PATH-REQUIRED", "api": [{"source": "inventory", "api": "GET /shared/42"}]},
        ]
        report = self.preview(cases, {"inventory": (before, removed), "records": (unchanged, unchanged)})
        self.assertEqual(report["analysisStatus"], "incomplete")
        self.assertEqual(report["affectedCaseIds"], ["INVENTORY"])
        self.assertEqual(report["unresolvedCaseIds"], ["AMBIGUOUS", "EXACT-PATH-REQUIRED"])
        by_id = {case["caseId"]: case for case in report["cases"]}
        ambiguous, = by_id["AMBIGUOUS"]["associations"]["primary"]["interfaces"]
        self.assertEqual(ambiguous["reason"], "ambiguous_source")
        self.assertEqual(ambiguous["candidateSources"], ["inventory", "records"])
        self.assertEqual(by_id["INVENTORY"]["retestReasons"][0]["role"], "primary")

    def test_compare_resolved_components_and_raw_numeric_types(self):
        paths = {"/items": {"get": {"responses": {"200": {"content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Counter"}},
        }}}}}}
        before = self.snapshot("before.json", paths, components={"schemas": {"Counter": {"enum": [True]}}})
        after = self.snapshot("after.json", paths, components={"schemas": {"Counter": {"enum": [1]}}})
        cases = [{"id": "ITEMS", "api": "GET /items"}]
        report = self.preview(cases, {"inventory": (before, after)})
        self.assertEqual(report["affectedCaseIds"], ["ITEMS"])
        self.assertEqual(report["cases"][0]["retestReasons"][0]["change"], "modified")
        paths["/items"]["post"] = {"summary": "An unrelated method on the same path"}
        after = self.snapshot("after.json", paths, components={"schemas": {"Counter": {"enum": [True]}}})
        report = self.preview(cases, {"inventory": (before, after)})
        self.assertEqual(report["affectedCaseIds"], [])
        self.assertEqual(report["sources"][0]["changes"], [{"method": "POST", "path": "/items", "change": "added"}])

    def test_missing_invalid_and_unresolved_snapshots_are_not_guessed(self):
        before = self.snapshot("before.json", {"/items": {"get": {}}})
        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"paths":{"/items":{"get":{},"get":{}}}}', encoding="utf-8")
        unknown = self.snapshot("unknown.json", {"/items": {"get": {"responses": {"200": {
            "$ref": "#/components/responses/NotInSnapshot",
        }}}}})
        cases = [{"id": "ITEMS", "api": "GET /items"}]
        for after in (None, self.root / "absent.json", duplicate, unknown):
            with self.subTest(after=after):
                report = self.preview(cases, {"inventory": (before, after)})
                self.assertEqual(report["analysisStatus"], "incomplete")
                self.assertEqual(report["affectedCaseIds"], [])
                self.assertEqual(report["unresolvedCaseIds"], ["ITEMS"])

    def test_legacy_runner_template_and_new_lists_validate_without_mixed_coverage(self):
        case = {"id": "ITEMS", "expect": "read", "path": "/items?limit=2", "mode": "safe",
                "suite": "inventory", "group": "read", "desc": "list", "expected": "schema",
                "fixtureApis": [{"source": "workbench", "api": "POST /fixtures"}]}
        runner_api = {"read": "GET {path}"}
        self.assertEqual(case_apis(case, runner_api), ["GET /items"])
        self.assertEqual(case_contract_bindings(case, runner_api)["fixture"]["bindings"], [
            {"source": "workbench", "method": "POST", "path": "/fixtures"},
        ])
        module = SimpleNamespace(CATALOG=[case], RUNNER_API=runner_api, RUNNERS={"read": None})
        with patch.dict(sys.modules, {"catalog": SimpleNamespace(MODULES=[module])}), \
                patch("validate_catalog.load_oas_paths", return_value=set()):
            self.assertEqual(check_structure()[0], [])
            case["fixtureApis"] = None
            self.assertTrue(any("ITEMS fixtureApis" in error for error in check_structure()[0]))

    def test_read_only_historical_evidence_is_scoped_and_never_current_pass(self):
        before = self.snapshot("before.json", {"/items": {"get": {}}})
        after = self.snapshot("after.json", {})
        cases = [{"id": "ITEMS", "api": "GET /items"}]
        snapshots = {"inventory": (before, after)}
        sha = self.preview(cases, snapshots)["sources"][0]["before"]["sha256"]
        db = self.root / "history.db"
        with patch.object(run_history, "DB_PATH", db):
            self.assertIsNone(run_history.read_run_evidence(1))
            self.assertFalse(db.exists())
            run_id = run_history.record_case({"id": "ITEMS", "ok": True, "status": "passed",
                                              "subs": [{"label": "shape", "ok": True, "status": "passed"}]}, metadata={
                "packId": "sample", "contractFingerprints": {"inventory": sha},
            })
            prior = db.read_bytes()
            evidence = run_history.read_run_evidence(run_id)
            self.assertEqual(db.read_bytes(), prior)
        report = self.preview(cases, snapshots, history=evidence)
        case, = report["cases"]
        self.assertEqual(case["history"]["status"], "passed")
        self.assertEqual(case["history"]["contractFingerprints"][0]["matches"], ["before"])
        self.assertFalse(report["historyEvidence"]["consumedSnapshotVerified"])
        self.assertEqual(case["verification"], {"ok": None, "status": "incomplete"})
        foreign = self.preview(cases, snapshots, history=dict(evidence, packId="different"))
        self.assertEqual(foreign["historyEvidence"]["reason"], "different_pack")
        self.assertNotIn("history", foreign["cases"][0])

    def test_old_history_is_read_without_schema_upgrade(self):
        db = self.root / "old-history.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript("""
                CREATE TABLE runs (id INTEGER PRIMARY KEY);
                CREATE TABLE case_results (id INTEGER PRIMARY KEY, run_id INTEGER, case_id TEXT, ok INTEGER);
                INSERT INTO runs VALUES (1);
                INSERT INTO case_results VALUES (1, 1, 'LEGACY', 1);
            """)
        prior = db.read_bytes()
        with patch.object(run_history, "DB_PATH", db):
            evidence = run_history.read_run_evidence(1)
        self.assertEqual(evidence["cases"], {"LEGACY": None})
        self.assertEqual(evidence["contractFingerprints"], {})
        self.assertEqual(db.read_bytes(), prior)

    def test_cli_reads_explicit_snapshots_in_selected_pack_without_history_creation(self):
        path = self.snapshot("sample.json", {"/items": {"get": {}}})
        data = self.root / "data"
        env = dict(os.environ, PLATFORM_ENV_FILE="/dev/null", PLATFORM_PACKS="demo_pack",
                   PLATFORM_PACK_DATA_DIR=str(data), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/contract_case_impact.py"),
                                 "--before", f"sample={path}", "--after", f"sample={path}"],
                                env=env, capture_output=True, text=True, timeout=30, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["pack"], "demo_pack")
        self.assertEqual(report["businessCasesExecuted"], 0)
        self.assertEqual(report["affectedCaseIds"], [])
        self.assertFalse(data.exists())
