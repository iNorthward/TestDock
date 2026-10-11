from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "test-platform", ROOT / "test-platform" / "common"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import db_conn  # noqa: E402
import platform_config  # noqa: E402


class DatabaseConfigTests(unittest.TestCase):
    def test_project_setting_prefers_neutral_key_then_legacy_alias_then_pack_default(self):
        defaults = {"PLATFORM_AUTH_BASE": "https://pack.example.test"}
        with patch.object(platform_config, "_PROJECT_DEFAULTS", defaults), \
                patch.dict(os.environ, {
                    "PLATFORM_AUTH_BASE": "https://project.example.test",
                    "EXAMPLE_PROJECT_AUTH_BASE": "https://legacy.example.test",
                }, clear=True):
            self.assertEqual(
                platform_config.project_setting(
                    "PLATFORM_AUTH_BASE", aliases=("EXAMPLE_PROJECT_AUTH_BASE",), default="",
                ),
                "https://project.example.test",
            )

        with patch.object(platform_config, "_PROJECT_DEFAULTS", defaults), \
                patch.dict(os.environ, {
                    "PLATFORM_AUTH_BASE": "",
                    "EXAMPLE_PROJECT_AUTH_BASE": "https://legacy.example.test",
                }, clear=True):
            self.assertEqual(
                platform_config.project_setting(
                    "PLATFORM_AUTH_BASE", aliases=("EXAMPLE_PROJECT_AUTH_BASE",), default="",
                ),
                "https://legacy.example.test",
            )

        with patch.object(platform_config, "_PROJECT_DEFAULTS", defaults), \
                patch.dict(os.environ, {"PLATFORM_AUTH_BASE": ""}, clear=True):
            self.assertEqual(
                platform_config.project_setting(
                    "PLATFORM_AUTH_BASE", aliases=("EXAMPLE_PROJECT_AUTH_BASE",), default="",
                ),
                "https://pack.example.test",
            )

    def test_empty_setting_without_pack_default_remains_empty(self):
        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, {"PLATFORM_AUTH_BASE": ""}, clear=True):
            self.assertEqual(
                platform_config.project_setting(
                    "PLATFORM_AUTH_BASE", aliases=("EXAMPLE_PROJECT_AUTH_BASE",), default="fallback",
                ),
                "",
            )

    @unittest.skipUnless(db_conn.pymysql, "optional pymysql is not installed")
    def test_connection_uses_neutral_environment_keys(self):
        env = {
            "PLATFORM_DB_HOST": "db.example.test",
            "PLATFORM_DB_PORT": "4406",
            "PLATFORM_DB_NAME": "project_data",
            "PLATFORM_DB_USER": "project-user",
            "PLATFORM_DB_PASSWORD": "project-password",
            "PLATFORM_DB_CONNECT_TIMEOUT": "7",
            "PLATFORM_DB_READ_TIMEOUT": "8",
            "PLATFORM_DB_WRITE_TIMEOUT": "9",
            "PLATFORM_DB_POOL_ENABLED": "0",
        }
        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, env, clear=True), \
                patch.object(db_conn.pymysql, "connect") as connect:
            db_conn.db()

        args = connect.call_args.kwargs
        self.assertEqual(args["host"], "db.example.test")
        self.assertEqual(args["port"], 4406)
        self.assertEqual(args["database"], "project_data")
        self.assertEqual(args["user"], "project-user")
        self.assertEqual(args["password"], "project-password")
        self.assertEqual(args["connect_timeout"], 7)
        self.assertEqual(args["read_timeout"], 8)
        self.assertEqual(args["write_timeout"], 9)

    def test_undeclared_legacy_credentials_are_not_used(self):
        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.object(platform_config, "_PROJECT_ALIASES", {}), \
                patch.dict(os.environ, {"DB_NAME": "legacy", "DB_USER": "legacy", "DB_PWD": "legacy"}, clear=True):
            settings = db_conn._db_settings()
        self.assertEqual((settings["name"], settings["user"], settings["password"]), ("", "", ""))

    def test_missing_database_name_has_no_core_sample_project_fallback(self):
        env = {
            "PLATFORM_DB_HOST": "db.example.test",
            "PLATFORM_DB_NAME": "",
            "PLATFORM_DB_USER": "user",
            "PLATFORM_DB_PASSWORD": "password",
        }
        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, env, clear=True), \
                self.assertRaisesRegex(RuntimeError, "PLATFORM_DB_NAME"):
            db_conn.db()

    def test_project_schema_path_resolves_relative_to_repository_and_legacy_alias(self):
        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, {"PLATFORM_SCHEMA_JSON": "schema/project.json"}, clear=True):
            self.assertEqual(
                platform_config.project_path("PLATFORM_SCHEMA_JSON"),
                ROOT / "schema" / "project.json",
            )

        with patch.object(platform_config, "_PROJECT_DEFAULTS", {}), \
                patch.dict(os.environ, {
                    "PLATFORM_SCHEMA_JSON": "",
                    "LEGACY_SCHEMA_PATH": "schema/legacy.json",
                }, clear=True):
            self.assertEqual(
                platform_config.project_path("PLATFORM_SCHEMA_JSON", aliases=("LEGACY_SCHEMA_PATH",)),
                ROOT / "schema" / "legacy.json",
            )

        with patch.object(platform_config, "_PROJECT_DEFAULTS", {
            "PLATFORM_SCHEMA_JSON": "db/default-schema.json",
        }), patch.dict(os.environ, {"PLATFORM_SCHEMA_JSON": ""}, clear=True):
            self.assertEqual(
                platform_config.project_path("PLATFORM_SCHEMA_JSON"),
                ROOT / "db" / "default-schema.json",
            )


if __name__ == "__main__":
    unittest.main()
