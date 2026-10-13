from contextlib import closing
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"test-platform"),str(ROOT/"test-platform/common")]
import execution_provenance as provenance
import pack_registry
import run_history as history


class ExecutionProvenanceTests(unittest.TestCase):
    def setUp(self):
        with provenance._RUNTIME_LOCK:
            previous=dict(provenance._RUNTIME_SNAPSHOTS)
            provenance._RUNTIME_SNAPSHOTS.clear()
        def restore():
            with provenance._RUNTIME_LOCK:
                provenance._RUNTIME_SNAPSHOTS.clear()
                provenance._RUNTIME_SNAPSHOTS.update(previous)
        self.addCleanup(restore)

    def initialize(self):
        provenance.initialize_runtime_snapshot(provenance.capture_code_snapshot())

    def test_adapter_changes_affect_selected_pack_fingerprint_but_foreign_adapters_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
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
                 patch.object(pack_registry,"REPO_ROOT",root), \
                 patch.object(provenance,"pool_path",return_value=root/"missing-pool.json"):
                self.initialize()
                first=provenance.execution_metadata()
                (root/"adapters/sample/decoder.py").write_text('version=2')
                second=provenance.execution_metadata()
                (root/"adapters/other/decoder.py").write_text('version=2')
                third=provenance.execution_metadata()
            self.assertEqual(first['codeFingerprint'],second['codeFingerprint'])
            self.assertNotEqual(first['diskCodeFingerprint'],second['diskCodeFingerprint'])
            self.assertEqual(second['diskCodeFingerprint'],third['diskCodeFingerprint'])
            self.assertTrue(second['codeEvidence']['restartRequired'])

    def test_explicit_case_dirs_and_adapter_files_outside_pack_are_fingerprinted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
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
                 patch.object(pack_registry,"REPO_ROOT",root), \
                 patch.object(provenance,"pool_path",return_value=root/"missing-pool.json"):
                self.initialize()
                first=provenance.execution_metadata()
                adapter.write_text('version=2')
                second=provenance.execution_metadata()
                case.write_text('version=2')
                third=provenance.execution_metadata()
            self.assertEqual(first['codeFingerprint'],second['codeFingerprint'])
            self.assertEqual(second['codeFingerprint'],third['codeFingerprint'])
            self.assertNotEqual(first['diskCodeFingerprint'],second['diskCodeFingerprint'])
            self.assertNotEqual(second['diskCodeFingerprint'],third['diskCodeFingerprint'])

    def test_fingerprints_follow_selected_code_contracts_and_redact_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
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
                 patch.object(pack_registry,"REPO_ROOT",root), \
                 patch.object(provenance,"project_path",return_value=contract), \
                 patch.object(provenance,"pool_path",return_value=pool):
                self.initialize()
                first=provenance.execution_metadata({"password":"SECRET","nested":{"token":"TOKEN","volume":"0.01"}})
                case.write_text('x=2')
                second=provenance.execution_metadata()
                contract.write_text('{"wire":"string"}')
                third=provenance.execution_metadata()
                (data/"secret.py").write_text('CHANGED_SECRET')
                fourth=provenance.execution_metadata()
                pool.write_text('{"identities":[{"id":"synthetic"}]}')
                fifth=provenance.execution_metadata()
            self.assertEqual(first["codeFingerprint"],second["codeFingerprint"])
            self.assertNotEqual(first["diskCodeFingerprint"],second["diskCodeFingerprint"])
            self.assertNotEqual(second["contractFingerprints"],third["contractFingerprints"])
            self.assertEqual(third["codeFingerprint"],fourth["codeFingerprint"])
            self.assertNotEqual(fourth["identityPoolFingerprint"],fifth["identityPoolFingerprint"])
            self.assertEqual(fourth["codeFingerprint"],fifth["codeFingerprint"])
            self.assertEqual(first["executionOptions"],{"nested":{"volume":"0.01"}})
            self.assertEqual(first['contractEvidence']['basis'],'disk-at-execution')

    def fixture(self, root):
        (root/'resources').mkdir()
        (root/'resources/platform-core-files.json').write_text('["core.py"]')
        (root/'core.py').write_text('version=1')
        pack_root=root/'packs/sample'
        pack_root.mkdir(parents=True)
        (pack_root/'pack.json').write_text('{}')
        helper=pack_root/'helper.py'
        helper.write_text('def run(): return 1')
        case=pack_root/'sample_cases.py'
        case.write_text('version=1')
        pack={'id':'sample','root':pack_root,'manifest_path':pack_root/'pack.json',
              'case_dirs':[pack_root]}
        stack=ExitStack()
        stack.enter_context(patch.object(provenance,'REPO_ROOT',root))
        stack.enter_context(patch.object(pack_registry,'REPO_ROOT',root))
        stack.enter_context(patch.object(provenance,'load_packs',return_value=[pack]))
        stack.enter_context(patch.object(provenance,'pool_path',return_value=root/'missing-pool.json'))
        return stack, helper, case

    def test_helper_keeps_loaded_version_when_disk_changes_and_cases_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            stack,helper,case=self.fixture(root)
            with stack:
                snapshot=provenance.capture_code_snapshot()
                loaded={}
                exec(compile(helper.read_text(),str(helper),'exec'),loaded)
                provenance.initialize_runtime_snapshot(snapshot)
                first=provenance.execution_metadata()
                helper.write_text('def run(): return 2')
                (root/'core.py').write_text('version=2')
                second=provenance.execution_metadata()
                self.assertEqual(loaded['run'](),1)
                self.assertEqual(first['codeFingerprint'],second['codeFingerprint'])
                self.assertNotEqual(first['diskCodeFingerprint'],second['diskCodeFingerprint'])
                case.write_text('version=2')
                before_reload=provenance.capture_code_snapshot()
                provenance.refresh_case_runtime_snapshot(before_reload,[case])
                third=provenance.execution_metadata()
                self.assertNotEqual(second['codeFingerprint'],third['codeFingerprint'])
                self.assertEqual(third['codeEvidence']['diskDriftSources'],['core.py','packs/sample/helper.py'])
                self.assertNotEqual(third['codeFingerprint'],third['diskCodeFingerprint'])
                self.assertTrue(third['codeEvidence']['restartRequired'])
                self.assertEqual(loaded['run'](),1)

    def test_case_reload_refreshes_only_case_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            stack,_helper,case=self.fixture(root)
            with stack:
                self.initialize()
                first=provenance.execution_metadata()
                case.write_text('version=2')
                self.assertEqual(first['codeFingerprint'],provenance.execution_metadata()['codeFingerprint'])
                provenance.refresh_case_runtime_snapshot(provenance.capture_code_snapshot(),[case])
                updated=provenance.execution_metadata()
                self.assertNotEqual(first['codeFingerprint'],updated['codeFingerprint'])
                self.assertEqual(updated['codeFingerprint'],updated['diskCodeFingerprint'])
                self.assertFalse(updated['codeDrift'])

    def test_sources_changing_across_load_boundary_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            stack,helper,case=self.fixture(root)
            with stack:
                snapshot=provenance.capture_code_snapshot()
                helper.write_text('def run(): return 2')
                provenance.initialize_runtime_snapshot(snapshot)
                initial=provenance.execution_metadata()
                self.assertIsNone(initial['codeFingerprint'])
                self.assertEqual(initial['codeEvidence']['uncertainSources'],['packs/sample/helper.py'])
                self.assertTrue(initial['codeEvidence']['restartRequired'])
                provenance.refresh_case_runtime_snapshot(provenance.capture_code_snapshot(),[case])
                self.assertIsNone(provenance.execution_metadata()['codeFingerprint'])
                # A new initialization boundary (new process) establishes fresh evidence.
                self.initialize()
                reload_snapshot=provenance.capture_code_snapshot()
                case.write_text('version=2')
                provenance.refresh_case_runtime_snapshot(reload_snapshot,[case])
                uncertain=provenance.execution_metadata()
                self.assertIsNone(uncertain['codeFingerprint'])
                self.assertEqual(uncertain['codeEvidence']['uncertainSources'],['packs/sample/sample_cases.py'])
                # A later stable successful case reload resolves this case-only uncertainty.
                provenance.refresh_case_runtime_snapshot(provenance.capture_code_snapshot(),[case])
                self.assertIsNotNone(provenance.execution_metadata()['codeFingerprint'])

    def test_missing_initialization_and_fork_do_not_claim_runtime_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            stack,_helper,_case=self.fixture(root)
            with stack:
                first=provenance.execution_metadata()
                self.assertIsNone(first['codeFingerprint'])
                self.assertIsNone(first['codeDrift'])
                self.assertEqual(first['codeEvidence']['status'],'unknown')
                self.assertTrue(first['diskCodeFingerprint'])
                self.initialize()
                self.assertIsNotNone(provenance.execution_metadata()['codeFingerprint'])
                with patch.object(provenance.os,'getpid',return_value=-1):
                    forked=provenance.execution_metadata()
                self.assertIsNone(forked['codeFingerprint'])

    def test_deleted_case_is_removed_only_after_successful_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            stack,_helper,case=self.fixture(root)
            with stack:
                self.initialize()
                before=provenance.execution_metadata()
                case.unlink()
                removed=provenance.execution_metadata()
                self.assertEqual(before['codeFingerprint'],removed['codeFingerprint'])
                self.assertTrue(removed['codeDrift'])
                provenance.refresh_case_runtime_snapshot(provenance.capture_code_snapshot(),[])
                after=provenance.execution_metadata()
                self.assertEqual(after['codeFingerprint'],after['diskCodeFingerprint'])
                self.assertFalse(after['codeDrift'])

    def test_history_round_trip_and_legacy_rows_have_explicit_empty_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(history,"DB_PATH",Path(directory)/"history.db"):
            # Build the old schema by omitting the new column; exercise ALTER.
            import sqlite3
            with closing(sqlite3.connect(history.DB_PATH)) as conn, conn:
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
