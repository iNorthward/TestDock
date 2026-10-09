import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apifox_contract_audit as aca
import db_schema_impact_audit as dsia
import run_apifox_inspection as rai
import sync_db_schema as sds
from report_hand_section import extract_hand_sections, preserve_hand_section, wrap_hand_section


class InspectionQualityTests(unittest.TestCase):
    def test_hand_section_preservation_and_migration(self):
        marker = wrap_hand_section("人工结论")
        self.assertEqual(extract_hand_sections(marker), "人工结论")
        rewritten = preserve_hand_section("# old\n" + marker, "# new\n" + wrap_hand_section(""))
        self.assertIn("人工结论", rewritten)
        self.assertEqual(extract_hand_sections("## 3. 已修复\n\n_本轮为自动巡检，修复项请在本节手工追加或关联 PR_\n"), "")
        migrated = preserve_hand_section("## 3. 已修复\n\n人工写的修复记录\n\n## 4. 待办", "# fresh\n" + wrap_hand_section(""), migrate_apifox=True)
        self.assertEqual(extract_hand_sections(migrated), "人工写的修复记录")
        placeholder = preserve_hand_section(
            "## 3. 已修复\n\n_（本轮为自动巡检，修复项请在本节手工追加或关联 PR）_\n\n## 4. 待办",
            "# fresh\n" + wrap_hand_section(""),
            migrate_apifox=True,
        )
        self.assertEqual(extract_hand_sections(placeholder), "在标记内追加结论")

    def test_parameter_fingerprint_constraints_ignore_description(self):
        local = {"paths": {"/mgr/example": {"get": {"parameters": [{"name": "q", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}}]}}}}
        changed = json.loads(json.dumps(local))
        changed["paths"]["/mgr/example"]["get"]["parameters"][0]["required"] = True
        self.assertEqual(len(aca.audit(local, changed, {("GET", "/mgr/example")})["contract_diffs"]), 1)
        desc = json.loads(json.dumps(local))
        desc["paths"]["/mgr/example"]["get"]["parameters"][0]["description"] = "new"
        self.assertEqual(aca.audit(local, desc, {("GET", "/mgr/example")})["contract_diffs"], [])

    def test_schema_required_is_constraint_change(self):
        def data(required):
            return {
                "paths": {"/mgr/a": {"get": {"responses": {"200": {"schema": {"$ref": "#/components/schemas/A"}}}}}},
                "components": {"schemas": {"A": {"properties": {"x": {"type": "string"}}, "required": required}}},
            }
        local, online = data([]), data(["x"])
        diffs = aca.diff_covered_schemas(local, online, aca.load_ops_from_oas(local), aca.load_ops_from_oas(online), {("GET", "/mgr/a")})
        self.assertEqual(diffs[0]["typeChanged"], [])
        self.assertEqual(diffs[0]["constraintChanged"][0]["field"], "x")

    def test_new_method_same_path_is_new_operation(self):
        local = {"paths": {"/mgr/example": {"get": {}}}}
        online = {"paths": {"/mgr/example": {"get": {}, "post": {}}}}
        result = aca.audit(local, online, set())
        self.assertIn("POST /mgr/example", result["new_in_apifox"])
        self.assertNotIn("GET /mgr/example", result["new_in_apifox"])

    def test_baseline_decision(self):
        self.assertFalse(rai.should_advance_baseline({"contract_diffs": [1]}, False))
        self.assertTrue(rai.should_advance_baseline({"new_in_apifox": [1]}, False))
        self.assertFalse(rai.should_advance_baseline({}, True))
        self.assertTrue(rai.should_advance_baseline({"contract_diffs": [1]}, False, True))

    def test_baseline_report_distinguishes_pending_from_completed_sync(self):
        audit = {
            "covered_count": 0, "local_trade_paths": 0, "apifox_trade_paths": 0,
            "contract_diffs": [], "schema_diffs": [], "missing_in_local": [],
            "missing_in_apifox": [], "removed_from_apifox": [], "new_in_apifox": [],
            "stale_in_local": [],
        }
        b = {"audit": audit, "summary": {"schemaDiffSample": []},
             "baselineAdvanced": False, "baselinePending": True}
        c = {"skipped": True, "skippedReason": "cli"}
        d = {"catalogLint": {}, "coverage": {}, "docSyncOk": True}

        pending = rai.render_report("today", b, c, d)
        self.assertIn("推进状态待确认", pending)
        self.assertNotIn("| 基线 | 已推进 |", pending)

        b["baselineAdvanced"] = True
        b["baselinePending"] = False
        advanced = rai.render_report("today", b, c, d)
        self.assertIn("| 基线 | 已推进 |", advanced)
        self.assertNotIn("推进状态待确认", advanced)

    def test_skipped_safe_not_converged(self):
        audit = {"covered_count": 0, "local_trade_paths": 0, "apifox_trade_paths": 0,
            "contract_diffs": [], "schema_diffs": [], "missing_in_local": [], "missing_in_apifox": [], "removed_from_apifox": [], "new_in_apifox": [], "stale_in_local": []}
        b = {"audit": audit, "summary": {"schemaDiffSample": []}}
        d = {"catalogLint": {}, "coverage": {}, "docSyncOk": True}
        report = rai.render_report("today", b, {"skipped": True, "skippedReason": "cli"}, d)
        self.assertIn("动态验证未执行", report)
        self.assertNotIn("均收敛", report)

        report = rai.render_report("today", b, {
            "skipped": False, "status": "incomplete", "passed": 2, "failed": 0,
            "skippedCount": 1, "incomplete": 0, "total": 3, "runId": 7,
            "failedIds": [], "skippedIds": ["K-R01"], "incompleteIds": [], "bySuite": {},
        }, d)
        self.assertIn("跳过 1", report)
        self.assertIn("SAFE 覆盖不完整", report)
        self.assertNotIn("SAFE 通过率 | 2/3 (67%) · 失败 0 · 跳过 0", report)

    def test_column_reference_neighborhood_and_field_map(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "demo_cases.py"
            fp.write_text('sql = "SELECT id FROM trade_records"\n' + "\n" * 10 + 'print(create_time)\n', encoding="utf-8")
            self.assertEqual(dsia._scan_column("trade_records", "create_time", [], [fp])[0], [])
            fp.write_text('sql = "SELECT create_time FROM trade_records"\n', encoding="utf-8")
            self.assertTrue(dsia._scan_column("trade_records", "create_time", [], [fp])[0])
            fp.write_text('x = upgrade_team_user_count\n', encoding="utf-8")
            self.assertEqual(dsia._scan_column("trade_records", "team_count", [], [fp])[0], [])
            fp.write_text('FIELD_MAP = {"readCount": "read_count"}\nsql = "SELECT id FROM trade_records"\n', encoding="utf-8")
            self.assertTrue(dsia._scan_column("trade_records", "read_count", [], [fp])[0])

    def test_field_map_hits_include_sibling_helpers(self):
        import gen_apifox_coverage_wiki as wiki
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "demo_cases.py").write_text("CATALOG = []\n", encoding="utf-8")
            (folder / "demo_query.py").write_text('PAGE_FIELD_MAP = {"readCount": "read_count"}\n', encoding="utf-8")
            rel = (folder / "demo_cases.py").resolve()
            original = wiki.discover_modules
            wiki.discover_modules = lambda: [("demo", "demo", str(rel), "")]
            try:
                hits = rai._field_map_hits(["demo"], ["read_count"])
            finally:
                wiki.discover_modules = original
            self.assertTrue(any(h["file"].endswith("demo_query.py") and h["field"] == "readCount" for h in hits))

    def test_added_column_with_sql_ref_is_review(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "demo_cases.py"
            fp.write_text('sql = "SELECT new_col FROM demo_table"\n', encoding="utf-8")
            impact = dsia.audit_impact({
                "new_tables": [], "removed_tables": [],
                "modified_tables": [{"name": "demo_table", "added_columns": ["new_col"], "removed_columns": [], "changed_columns": []}],
            }, [fp])
            col = impact["impacts"][0]["columnImpacts"][0]
            self.assertEqual(col["severity"], "review")
            self.assertTrue(col["fileRefs"])

    def test_unrelated_column_token_does_not_override_real_ref(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real_cases.py"
            real.write_text('sql = "SELECT create_time FROM trade_records"\n', encoding="utf-8")
            noise = Path(td) / "panel.html"
            noise.write_text("<div>create_time</div>\n", encoding="utf-8")
            impact = dsia.audit_impact({
                "new_tables": [], "removed_tables": [],
                "modified_tables": [{"name": "trade_records", "added_columns": [], "removed_columns": ["create_time"], "changed_columns": []}],
            }, [real, noise])
            col = impact["impacts"][0]["columnImpacts"][0]
            self.assertEqual(col["severity"], "must_fix")
            self.assertEqual(col["ambiguousRefs"], [])
            self.assertEqual([Path(r["file"]).name for r in col["fileRefs"]], ["real_cases.py"])
            noise_only = dsia.audit_impact({
                "new_tables": [], "removed_tables": [],
                "modified_tables": [{"name": "trade_records", "added_columns": [], "removed_columns": [], "changed_columns": [
                    {"name": "create_time", "indexOnly": False, "before": {"type": "datetime", "key": ""}, "after": {"type": "datetime(3)", "key": ""}}
                ]}],
            }, [noise])
            changed = noise_only["impacts"][0]["columnImpacts"][0]
            self.assertEqual(changed["severity"], "cache_only")
            self.assertEqual(changed["ambiguousRefs"], [])

    def test_column_severity_classification(self):
        sev, _ = dsia._column_severity("changed_column", [{}], index_only=True)
        self.assertEqual(sev, "cache_only")
        self.assertEqual(dsia._column_severity("changed_column", [{}])[0], "review")
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "demo.py"
            fp.write_text("pass", encoding="utf-8")
            impact = dsia.audit_impact({"new_tables": ["new_table"], "removed_tables": [], "modified_tables": []}, [fp])
            self.assertEqual(impact["impacts"][0]["severity"], "optional")
            fp.write_text('sql = "SELECT id FROM new_table"\n', encoding="utf-8")
            impact = dsia.audit_impact({"new_tables": ["new_table"], "removed_tables": [], "modified_tables": []}, [fp])
            self.assertEqual(impact["impacts"][0]["severity"], "review")

    def test_index_changes_are_structural_and_not_column_type(self):
        col = {"COLUMN_NAME": "id", "COLUMN_TYPE": "bigint", "IS_NULLABLE": "NO", "COLUMN_DEFAULT": None, "EXTRA": "", "COLUMN_KEY": "MUL"}
        old = {"tables": [{"name": "t", "columns": [dict(col, COLUMN_KEY="")], "indexes": [{"INDEX_NAME": "ix", "NON_UNIQUE": 1, "COLS": "id"}], "fks": []}]}
        new = {"tables": [{"name": "t", "columns": [col], "indexes": [{"INDEX_NAME": "ix", "NON_UNIQUE": 1, "COLS": "id,x"}], "fks": []}]}
        diff = sds.compare_schemas(new, old)
        self.assertTrue(sds._schema_diff_has_changes(diff))
        self.assertTrue(diff["modified_tables"][0]["changed_columns"][0]["indexOnly"])
        self.assertTrue(diff["index_changes"])

    def test_db_impact_dry_run_counts_index_and_fk_only_changes(self):
        for changed_key, expected in (("index_changes", True), ("fk_changes", True), (None, False)):
            with self.subTest(changed_key=changed_key):
                diff = {"new_tables": [], "removed_tables": [], "modified_tables": [],
                        "index_changes": [], "fk_changes": []}
                if changed_key:
                    diff[changed_key] = [{"table": "t", "added": [("new",)], "removed": []}]
                sync_result = {"database": "db", "total_tables": 1, "cache_exists": True,
                               "cache_file": "packs/sample_project/db/test_sample_project-schema.json", "diff": diff}
                with patch.object(sys, "argv", ["db_schema_impact_audit.py", "--dry-run", "--write-report", "--json"]), \
                     patch.object(sds, "sync_schema", return_value=sync_result), \
                     patch.object(sds, "load_cached_schema", return_value={"tables": [{}]}), \
                     patch.object(dsia, "run_audit", return_value={"ok": True}) as audit_mock, \
                     redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as done:
                        dsia.main()
                self.assertEqual(done.exception.code, 0)
                self.assertEqual(audit_mock.call_args.args[0]["has_changes"], expected)
                self.assertTrue(audit_mock.call_args.kwargs["write_report"])

    def test_digest_stable_and_ignores_row_estimates(self):
        p = {"database": "db", "has_changes": True, "diff": {"modified_tables": [{"name": "t", "rows_est": 4, "added_columns": [], "removed_columns": [], "changed_columns": []}]}}
        a = dsia.run_audit(p.copy())
        p["diff"]["modified_tables"][0]["rows_est"] = 999
        b = dsia.run_audit(p.copy())
        self.assertEqual(a["diffDigest"], b["diffDigest"])


if __name__ == "__main__":
    unittest.main()
