"""Schema impact scanning follows the selected Pack's migrated sources."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import db_schema_impact_audit as audit


class SchemaImpactPackScopeTests(unittest.TestCase):
    def test_scan_includes_active_pack_external_cases_and_adapter_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sources = ["packs/acme/helpers/query.py", "packs/acme/cases/read_cases.py",
                       "examples/acme/read_cases.py", "adapters/acme/auth.py", "adapters/acme/codec.py",
                       "packs/other/cases/read_cases.py", "adapters/other/auth.py"]
            for source in sources:
                path = root / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SQL = "SELECT id FROM acme_balance"')
            pack = {"id": "acme", "root": root / "packs/acme", "case_dirs": ["packs/acme/cases", "examples/acme"],
                    "adapter_files": ["adapters/acme/auth.py"]}
            with patch.object(audit, "ROOT", root), patch.object(audit, "SCAN_DIRS", audit._scan_dirs(root, [pack])):
                files = audit._iter_py_files()
                self.assertEqual(files, sorted(root / source for source in sources[:5]))
                matches = audit._scan_table("acme_balance", files)
                self.assertEqual(len(matches), 5)
