"""Contract tools use Pack policy instead of assuming Example Project routes and fields."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import apifox_contract_audit as audit
import gen_interface_profile as profile


class ContractToolsPackScopeTests(unittest.TestCase):
    def test_unconfigured_project_includes_non_sample_project_operations(self):
        local = {"paths": {"/v1/orders": {"get": {}}}}
        online = {"paths": {"/v1/accounts": {"get": {}}}}
        with patch.object(audit, "CONTRACT_PATH_PREFIXES", ()):
            result = audit.audit(local, online, set())
            self.assertEqual(result["new_in_apifox"], ["GET /v1/accounts"])
            self.assertEqual(result["stale_in_local"], ["GET /v1/orders"])
            self.assertEqual(audit.removed_from_apifox(local, online)[0]["path"], "/v1/orders")

    def test_pack_declared_prefix_limits_scope(self):
        local = {"paths": {"/v1/orders": {"get": {}}, "/health": {"get": {}}}}
        with patch.object(audit, "CONTRACT_PATH_PREFIXES", ("/v1/",)):
            self.assertEqual(len(audit.removed_from_apifox(local, {"paths": {}})), 1)

    def test_prefix_configuration_rejects_invalid_values(self):
        for value in ("v1/", "/v1/,", ["/v1/"]):
            with self.subTest(value=value), patch.object(audit, "project_input_setting", return_value=value):
                with self.assertRaises(ValueError):
                    audit.contract_path_prefixes()
        with patch.object(audit, "project_input_setting", return_value=""):
            self.assertEqual(audit.contract_path_prefixes(), ())

    def test_generic_profile_does_not_invent_user_uid_rules(self):
        with patch.object(profile, "project_input_setting", side_effect=lambda key, default=None: default), \
                patch.object(profile, "summary_from_coverage", return_value=""):
            text = profile.render("GET", "/mgr/orders", {}, {})
        self.assertNotIn("userId↔UID", text)
        self.assertNotIn("skip_user_uid_base_rule", text)
        self.assertIn("待确认：依据所选 Pack", text)

    def test_profile_includes_explicit_pack_guidance(self):
        with patch.object(profile, "project_input_setting", return_value="Example project rule"), \
                patch.object(profile, "summary_from_coverage", return_value=""):
            text = profile.render("GET", "/v1/orders", {}, {})
        self.assertIn("Example project rule", text)
