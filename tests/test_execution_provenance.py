import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"test-platform"),str(ROOT/"test-platform/common")]
import execution_provenance as provenance
import run_history as history


class ExecutionProvenanceTests(unittest.TestCase):
    def test_adapter_changes_affect_selected_pack_fingerprint_but_foreign_adapters_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"resources").mkdir()
            (root/"resources/platform-core-files.json").write_text('[]')
            pack_root=root/"packs/sample"
            pack_root.mkdir(parents=True)
            manifest=pack_root/"pack.json"
            manifest.write_text('{}')
            for name in ('sample', 'other'):
                (root/"adapters"/name).mkdir(parents=True)
                (root/"adapters"/name/"decoder.py").write_text('version=1')
            pack={"id":"sample","root":pack_root,"manifest_path":manifest}
            with patch.object(provenance,"REPO_ROOT",root), patch.object(provenance,"load_packs",return_value=[pack]), \
                 patch.object(provenance,"pool_path",return_value=root/"missing-pool.json"):
                first=provenance.execution_metadata()
                (root/"adapters/sample/decoder.py").write_text('version=2')
                second=provenance.execution_metadata()
                (root/"adapters/other/decoder.py").write_text('version=2')
                third=provenance.execution_metadata()
            self.assertNotEqual(first['codeFingerprint'],second['codeFingerprint'])
            self.assertEqual(second['codeFingerprint'],third['codeFingerprint'])

    def test_explicit_case_dirs_and_adapter_files_outside_pack_are_fingerprinted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"resources").mkdir()
            (root/"resources/platform-core-files.json").write_text('[]')
            pack_root=root/"packs/sample"
            pack_root.mkdir(parents=True)
            manifest=pack_root/"pack.json"
            manifest.write_text('{}')
            cases_root=root/"registered_cases"
            cases_root.mkdir()
            case=cases_root/"sample_cases.py"
            case.write_text('version=1')
            adapter=root/"registered_adapter.py"
            adapter.write_text('version=1')
            pack={"id":"sample","root":pack_root,"manifest_path":manifest,
                  "case_dirs":[cases_root],"adapter_files":["registered_adapter.py"]}
            with patch.object(provenance,"REPO_ROOT",root), patch.object(provenance,"load_packs",return_value=[pack]), \
                 patch.object(provenance,"pool_path",return_value=root/"missing-pool.json"):
                first=provenance.execution_metadata()
                adapter.write_text('version=2')
                second=provenance.execution_metadata()
                case.write_text('version=2')
                third=provenance.execution_metadata()
            self.assertNotEqual(first['codeFingerprint'],second['codeFingerprint'])
            self.assertNotEqual(second['codeFingerprint'],third['codeFingerprint'])

    def test_fingerprints_follow_selected_code_contracts_and_redact_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"resources").mkdir()
            (root/"resources/platform-core-files.json").write_text('["core.py"]')
            (root/"core.py").write_text('x=1')
            pack_root=root/"packs/sample"
            pack_root.mkdir(parents=True)
            manifest=pack_root/"pack.json"
            manifest.write_text('{}')
            case=pack_root/"sample_cases.py"
            case.write_text('x=1')
            data=pack_root/"data"
            data.mkdir()
            (data/"secret.py").write_text('SYNTHETIC_SECRET')
            contract=root/"wire.json"
            contract.write_text('{}')
            pool=root/"identity_pool.json"
            pool.write_text('{"identities":[]}')
            pack={"id":"sample","root":pack_root,"manifest_path":manifest,
                  "execution_contract_sources":{"wire":"SAMPLE_WIRE"}}
            with patch.object(provenance,"REPO_ROOT",root),patch.object(provenance,"load_packs",return_value=[pack]), \
                 patch.object(provenance,"project_path",return_value=contract), \
                 patch.object(provenance,"pool_path",return_value=pool):
                first=provenance.execution_metadata({"password":"SECRET","nested":{"token":"TOKEN","volume":"0.01"}})
                case.write_text('x=2')
                second=provenance.execution_metadata()
                contract.write_text('{"wire":"string"}')
                third=provenance.execution_metadata()
                (data/"secret.py").write_text('CHANGED_SECRET')
                fourth=provenance.execution_metadata()
                pool.write_text('{"identities":[{"id":"synthetic"}]}')
                fifth=provenance.execution_metadata()
            self.assertNotEqual(first["codeFingerprint"],second["codeFingerprint"])
            self.assertNotEqual(second["contractFingerprints"],third["contractFingerprints"])
            self.assertEqual(third["codeFingerprint"],fourth["codeFingerprint"])
            self.assertNotEqual(fourth["identityPoolFingerprint"],fifth["identityPoolFingerprint"])
            self.assertEqual(fourth["codeFingerprint"],fifth["codeFingerprint"])
            self.assertEqual(first["executionOptions"],{"nested":{"volume":"0.01"}})

    def test_history_round_trip_and_legacy_rows_have_explicit_empty_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(history,"DB_PATH",Path(directory)/"history.db"):
            # Build the old schema by omitting the new column; exercise ALTER.
            import sqlite3
            with sqlite3.connect(history.DB_PATH) as conn:
                conn.executescript(history._SCHEMA.replace(",\n    metadata_json TEXT NOT NULL DEFAULT '{}'",""))
                conn.execute("INSERT INTO runs(ts,mode,source,passed,total,status) VALUES (1,'safe','legacy',0,0,'incomplete')")
            meta={"packId":"sample","codeFingerprint":"abc","contractFingerprints":{"wire":"def"},
                  "executionOptions":{"password":"SECRET","volume":"0.01"}}
            identifier=history.record_case({"id":"S-R01","ok":True},metadata=meta)
            detail=history.run_detail(identifier)
            expected={**meta,"executionOptions":{"volume":"0.01"}}
            self.assertEqual(detail["executionMetadata"],expected)
            self.assertEqual(history.case_history("S-R01")[0]["executionMetadata"],expected)
            self.assertEqual(history.recent_runs()[-1]["executionMetadata"],{})
            self.assertNotIn("SECRET",str(detail))

    def test_non_panel_history_captures_provenance_when_caller_has_no_metadata(self):
        meta = {"packId": "sample", "codeFingerprint": "synthetic"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(history, "DB_PATH", Path(directory)/"history.db"), \
             patch.object(provenance, "execution_metadata", return_value=meta) as capture:
            identifier = history.record_case({"id": "S-R01", "ok": True}, source="nightly")
            self.assertEqual(history.run_detail(identifier)["executionMetadata"], meta)
            self.assertEqual(capture.call_args.args[0]["source"], "nightly")
            identifier = history.record_case({"id": "S-R02", "ok": True}, metadata={})
            self.assertEqual(history.run_detail(identifier)["executionMetadata"], {})
        capture.assert_called_once()
