from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_PLATFORM = ROOT / "test-platform"


class CatalogImportBootstrapTest(unittest.TestCase):
    def test_catalog_imports_with_only_test_platform_on_pythonpath(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(TEST_PLATFORM)
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-c", "import catalog; assert catalog.SUITES"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
