from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test-platform" / "common"))


class EnvConfigLazyDefaultsTests(unittest.TestCase):
    def test_module_attributes_pick_up_pack_defaults_registered_after_import(self):
        import platform_config

        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, {
                    "PLATFORM_API_BASE": "",
                    "PLATFORM_AUTH_BASE": "",
                    "PLATFORM_CLI_TENANT": "",
                    "PLATFORM_MGR_TENANT": "",
                    "PLATFORM_CLI_USER": "",
                    "PLATFORM_MGR_USER": "",
                }, clear=False):
            import env_config

            importlib.reload(env_config)
            self.assertEqual(env_config.API, "")
            self.assertEqual(env_config.CLI_TENANT, "")

            platform_config.register_project_defaults({
                "PLATFORM_API_BASE": "https://example.test/api",
                "PLATFORM_AUTH_BASE": "https://auth.example.test",
                "PLATFORM_CLI_TENANT": "111",
                "PLATFORM_MGR_TENANT": "222",
            })

            self.assertEqual(env_config.API, "https://example.test/api")
            self.assertEqual(env_config.AUTH_BASE, "https://auth.example.test")
            self.assertEqual(env_config.CLI_TENANT, "111")
            self.assertEqual(env_config.MGR_TENANT, "222")
            self.assertIn("（111）", env_config.cli_account_label())


if __name__ == "__main__":
    unittest.main()
