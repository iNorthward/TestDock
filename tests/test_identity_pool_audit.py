from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "test-platform"))

import identity_pool_audit_core as auditlib  # noqa: E402
import identity_pool_audit as auditcli  # noqa: E402
import identity_pool as pool  # noqa: E402
import sync_agent_rules  # noqa: E402


class IdentityPoolAuditTests(unittest.TestCase):
    def _project(self, root: Path):
        pack_root = root / "packs" / "demo"
        (pack_root / "data").mkdir(parents=True)
        (root / "test-platform").mkdir()
        pack = {
            "id": "demo",
            "identity_pool_file": "data/identity_pool.json",
            "identity_alias_policy": {"category": "user", "role": "default_client_login", "source_kind": "cli_user"},
            "identity_requirements": {
                "user": ["required_user"], "agent": [], "admin": [], "scenario": ["positive_sample"],
                "fixture_templates": {"demo.nonexistent_record": {"id": 9223372036854000000}},
                "scenario_fields": {
                    "demo.nonexistent_record": ["id"],
                    "demo.auth_probe": ["invite_code"],
                    "positive_sample": ["record_id"],
                },
                "scenario_refs": {"positive_sample": ["user"]},
                "notes": {"fixture_name_pattern": "nonexistent|auth_probe", "role_meta": {"agent_parent": {
                    "title": "上级代理", "purpose": "Demo 邀请人",
                    "needs": {"fields": [], "refs": [], "preconditions": ["核验 inviter 关系"]},
                }}},
            },
        }
        (pack_root / "pack.json").write_text(json.dumps(pack), encoding="utf-8")
        pool = {
            "version": 1,
            "identities": [{"id": "user-1", "category": "user", "role": "default_client_login",
                            "roles": ["default_client_login"], "username": "test-login", "password": "secret"},
                           {"id": "agent-1", "category": "agent", "role": "agent_self"}],
            "scenarios": [], "fixtures": [],
        }
        (pack_root / "data" / "identity_pool.json").write_text(json.dumps(pool), encoding="utf-8")
        source = '''
from common.env_config import cli_user
from identity_pool import get_identity
DEFAULT_CLI = cli_user("module_alias")
ISOLATED = get_identity("user", "isolated_write_user")
CATALOG = [
    {"id": "DEMO-NEG-1", "fixture_roles": [{"category": "agent", "role": "agent_parent"}],
     "fixture_scenario": "demo.nonexistent_record"},
    {"id": "DEMO-POS-1", "fixture_scenario": "positive_sample"},
]
'''
        (root / "test-platform" / "example_cases.py").write_text(source, encoding="utf-8")
        return pack, pool

    def test_audit_finds_required_and_static_roles_and_scenarios(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._project(root)
            report, _, _, _ = auditlib.audit_root(root, "demo")
        missing_roles = {(row["category"], row["role"]) for row in report["missing_roles"]}
        self.assertIn(("user", "required_user"), missing_roles)
        self.assertIn(("user", "module_alias"), missing_roles)
        self.assertIn(("user", "isolated_write_user"), missing_roles)
        self.assertIn(("agent", "agent_parent"), missing_roles)
        self.assertEqual([row["name"] for row in report["missing_fixtures"]], ["demo.nonexistent_record"])
        self.assertEqual([row["name"] for row in report["missing_scenarios"]], ["positive_sample"])
        role = next(row for row in report["missing_roles"] if row["role"] == "agent_parent")
        self.assertIn("上级代理", role["title"])
        self.assertIn("inviter", " ".join(role["needs"]["preconditions"]))
        self.assertIn("DEMO-NEG-1", role["cases"])
        self.assertIn("example_cases", role["suites"])
        positive = report["missing_scenarios"][0]
        self.assertEqual(positive["needs"]["fields"], ["record_id"])
        self.assertEqual(positive["needs"]["refs"], ["user"])
        self.assertEqual(positive["purpose"], "使用位置待配置")
        self.assertIn("只读 SELECT", positive["suggestion"])
        self.assertIn("用户确认", positive["suggestion"])
        for finding in report["findings"]:
            self.assertTrue(finding["title"])
            self.assertTrue(finding["purpose"])
            self.assertEqual(set(finding["needs"]), {"fields", "refs", "preconditions"})

        from io import StringIO
        import contextlib
        output = StringIO()
        with contextlib.redirect_stdout(output):
            auditcli._print_audit(report)
        cli_text = output.getvalue()
        self.assertIn("是什么 / 用途：", cli_text)
        self.assertIn("需要什么：", cli_text)
        self.assertIn("使用位置待配置", cli_text)

    def test_existing_incomplete_and_duplicate_entries_also_have_shared_descriptions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, pool = self._project(root)
            pool["scenarios"] = [{"id": "scene-1", "name": "positive_sample", "data": {}, "identity_refs": {}}]
            pool["fixtures"] = [{"id": "fixture-1", "name": "positive_sample", "data": {}}]
            report, _, _, _ = auditlib.audit_root(root, "demo", pool=pool)
        self.assertEqual(len(report["incomplete_entries"]), 2)
        self.assertEqual(len(report["duplicate_names"]), 1)
        for finding in report["findings"]:
            self.assertTrue(finding["title"])
            self.assertTrue(finding["purpose"])
            self.assertEqual(set(finding["needs"]), {"fields", "refs", "preconditions"})




    def test_apply_safe_changes_only_alias_roles_and_declared_negative_fixtures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, original = self._project(root)
            report, pack, pool, scan = auditlib.audit_root(root, "demo")
            plan = auditlib.plan_safe_changes(pack=pack, pool=pool, audit=report, code_scan=scan)
            self.assertEqual([row["role"] for row in plan["append_roles"]], ["module_alias"])
            self.assertEqual([row["name"] for row in plan["add_fixtures"]], ["demo.nonexistent_record"])
            self.assertNotIn("positive_sample", [row["name"] for row in plan["add_fixtures"]])
            updated, applied = auditlib.apply_changes(pool, plan)

        self.assertEqual(len(applied), 2)
        self.assertEqual(original["identities"][0]["roles"], ["default_client_login"])
        self.assertEqual(updated["identities"][0]["roles"], ["default_client_login", "module_alias"])
        self.assertEqual(updated["identities"][0]["password"], "secret")
        self.assertEqual(updated["identities"][1], original["identities"][1])
        self.assertEqual([row["name"] for row in updated["fixtures"]], ["demo.nonexistent_record"])
        self.assertEqual(updated["fixtures"][0]["data"]["id"], 9223372036854000000)
        self.assertEqual(updated["scenarios"], original["scenarios"])



if __name__ == "__main__":
    unittest.main()
