from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test-platform"))
sys.path.insert(0, str(ROOT / "scripts"))

import platform_config as config
import timezone_config as tz
import project_paths
import project_inputs


class ProjectConfigIsolationTest(unittest.TestCase):
    def test_python_uses_selected_file_and_preserves_process_values(self):
        with tempfile.TemporaryDirectory() as td:
            selected, legacy = Path(td) / "selected.env", Path(td) / "legacy.env"
            selected.write_text("PLATFORM_PACKS=other\nSELECTED_ONLY=selected\nKEEP_EMPTY=file\nPLATFORM_ENV_FILE=legacy.env\n")
            legacy.write_text("LEGACY_ONLY=legacy\nTZ=UTC\n")
            with patch.dict(os.environ, {"PLATFORM_ENV_FILE": str(selected), "PLATFORM_PACKS": "demo_pack",
                                         "KEEP_EMPTY": ""}, clear=True), \
                    patch.object(config, "ENV_FILE", legacy), patch.object(tz, "ENV_FILE", legacy):
                config.load_project_env()
                tz.now_project_iso()
                self.assertEqual(os.environ["PLATFORM_PACKS"], "demo_pack")
                self.assertEqual(os.environ["SELECTED_ONLY"], "selected")
                self.assertEqual(os.environ["KEEP_EMPTY"], "")
                self.assertEqual(os.environ["PLATFORM_ENV_FILE"], str(selected))
                self.assertNotIn("LEGACY_ONLY", os.environ)

    def test_dev_null_disables_default_file_in_python_and_timezone(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "legacy.env"
            legacy.write_text("LEGACY_ONLY=legacy\nTZ=UTC\n")
            with patch.dict(os.environ, {"PLATFORM_ENV_FILE": "/dev/null"}, clear=True), \
                    patch.object(config, "ENV_FILE", legacy), patch.object(tz, "ENV_FILE", legacy):
                config.load_project_env()
                tz.configure_timezone()
                self.assertNotIn("LEGACY_ONLY", os.environ)
                self.assertEqual(os.environ["TZ"], "Asia/Shanghai")

    def test_explicit_file_argument_overrides_selector(self):
        with tempfile.TemporaryDirectory() as td:
            explicit = Path(td) / "explicit.env"
            explicit.write_text("EXPLICIT_ONLY=yes\n")
            with patch.dict(os.environ, {"PLATFORM_ENV_FILE": "/dev/null"}, clear=True):
                config.load_project_env(explicit)
                self.assertEqual(os.environ["EXPLICIT_ONLY"], "yes")

    def test_relative_selector_is_repository_relative(self):
        with patch.dict(os.environ, {"PLATFORM_ENV_FILE": "config/demo.env"}):
            self.assertEqual(tz.configured_env_file(), ROOT / "config/demo.env")

    def test_shell_and_python_share_process_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "project.env"
            env_file.write_text("PLATFORM_PACKS=sample_project\nKEEP_EMPTY=file\nFILE_ONLY=plain\nPLATFORM_ENV_FILE=other.env\n")
            child_env = {"PATH": os.environ["PATH"], "PLATFORM_ENV_FILE": str(env_file),
                         "PLATFORM_PACKS": "demo_pack", "KEEP_EMPTY": ""}
            for shell in ("bash", "zsh"):
                if not shutil.which(shell):
                    continue
                with self.subTest(shell=shell):
                    result = subprocess.run(
                        [shell, "-c", 'export PLATFORM_ENV_FILE="$2"; source "$1"; printf "%s|%s|%s|%s" "$PLATFORM_PACKS" "$KEEP_EMPTY" "$FILE_ONLY" "$PLATFORM_ENV_FILE"',
                         shell, str(ROOT / "scripts/load_env.sh"), str(env_file)],
                        env=child_env, cwd=td, capture_output=True, text=True, timeout=10, check=True,
                    )
                    self.assertEqual(result.stdout, f"demo_pack||plain|{env_file}")

    def test_runtime_and_cli_helpers_reject_resource_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "owned"
            root.mkdir()
            outside = Path(td) / "outside"
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            for helper in (project_paths.business_knowledge_path, project_paths.pack_data_path,
                           project_paths.artifact_path, project_inputs.business_knowledge_path,
                           project_inputs.pack_data_path, project_inputs.artifact_path):
                module = project_inputs if helper.__module__ == "project_inputs" else project_paths
                method = "project_input_path" if module is project_inputs else "project_path"
                with patch.object(module, method, return_value=root):
                    self.assertEqual(helper("nested", "file.json"), root.resolve() / "nested/file.json")
                    for part in ("../other/file.json", str(outside / "file.json"), "linked/file.json"):
                        with self.subTest(helper=helper.__name__, part=part), self.assertRaises(ValueError):
                            helper(part)


if __name__ == "__main__":
    unittest.main()
