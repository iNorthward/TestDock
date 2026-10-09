import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import targeted_regression as gate
import check_case_doc_sync as docs


class TargetedRegressionTests(unittest.TestCase):

    def test_unregistered_contract_resource_blocks_instead_of_running_zero_tests(self):
        groups=gate.load_groups()
        for relative in ("resources/new-contract.json","resources/new-contract.yaml","resources/new-schema.sql"):
            with self.subTest(relative=relative):
                plan=gate.build_plan(groups,[relative])
                self.assertEqual(plan["unmappedSources"],[relative])
                with self.assertRaisesRegex(ValueError,"缺回归映射"):
                    gate.execute_plan(plan)



    def test_resource_patterns_and_exclusions_cannot_escape_or_suppress_unmapped_code(self):
        groups=gate.RegressionGroups({"one":{"sources":["core/*.py"],"exclude_sources":["core/new.py"],"python":["test_one"]}},required_sources=["config/*.json"])
        plan=gate.build_plan(groups,["core/new.py","config/new.json","docs/report.json","panel.html"])
        self.assertEqual(plan["unmappedSources"],["core/new.py","config/new.json","panel.html"])
        self.assertNotIn("docs/report.json",plan["unmappedSources"])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"tests").mkdir()
            (root/"tests/test_one.py").write_text('pass')
            for field,value in (("required_sources",["../outside.json"]),("required_sources","*.json")):
                path=root/"regression.json"
                path.write_text(json.dumps({"version":1,field:value,"groups":{"one":{"python":["test_one"]}}}))
                with self.assertRaises(ValueError):gate.read_groups(path,root)

    def test_demo_gate_does_not_borrow_sample_project_contract_or_adapter_groups(self):
        environment={key:value for key,value in os.environ.items() if not key.startswith(("PLATFORM_","EXAMPLE_PROJECT_","APIFOX_"))}
        environment.update({"PLATFORM_PACKS":"demo_pack","PLATFORM_ENV_FILE":"/dev/null","PYTHONDONTWRITEBYTECODE":"1"})
        result=subprocess.run([sys.executable,str(ROOT/"scripts/targeted_regression.py"),"--plan","--file","packs/sample_project/resources/PLATFORM所有接口.openapi.json"],
                              cwd=ROOT,env=environment,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn("缺回归映射",result.stderr)
        self.assertNotIn("sample_project.contracts",result.stdout)

    def test_selection_runs_only_mapped_groups_and_changed_tests(self):
        groups={"one":{"sources":["core/one.py"],"python":["test_one"]},
                "two":{"sources":["core/two.py"],"python":["test_two"]}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"tests").mkdir()
            (root/"tests/test_changed.py").write_text('pass')
            plan=gate.build_plan(groups,["core/one.py","tests/test_changed.py","docs/report.md"],root=root)
        self.assertEqual(plan["groups"],["one"])
        self.assertEqual(plan["python"],["test_changed","test_one"])
        self.assertNotIn("test_two",plan["python"])
        self.assertFalse(plan["unmappedSources"])

    def test_unmapped_code_fails_before_test_execution_and_unknown_group_rejects(self):
        plan=gate.build_plan({},["core/new.py"])
        with patch("offline_process.execute") as loader:
            with self.assertRaisesRegex(ValueError,"缺回归映射"): gate.execute_plan(plan)
        loader.assert_not_called()
        with self.assertRaises(ValueError): gate.build_plan({},[],["unknown"])

    def test_manifest_rejects_discovery_names_missing_tests_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/"regression.json"
            for group in ({"python":["discover"]},{"python":["test_missing"]},
                          {"sources":["../outside.py"],"javascript":["tests/x.js"]}):
                path.write_text(json.dumps({"version":1,"groups":{"one":group}}))
                with self.assertRaises(ValueError): gate.read_groups(path,root)

    def test_node_commonjs_and_esm_sources_require_mapping_and_tests_are_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"tests").mkdir()
            for suffix in (".cjs", ".mjs"):
                relative="tests/test_probe"+suffix
                (root/relative).write_text('/* synthetic */')
                plan=gate.build_plan({},["core/new"+suffix,relative],root=root)
                self.assertEqual(plan["unmappedSources"],["core/new"+suffix])
                self.assertEqual(plan["javascript"],[relative])

    def test_offline_fixtures_cannot_inherit_real_secrets_or_override_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"tests").mkdir()
            (root/"tests/test_probe.py").write_text('pass')
            path=root/"regression.json"
            for fixtures in ({"PACK_BASIC":"parent-credential"}, {"PLATFORM_ENV_FILE":"synthetic-offline-path"},
                             {"NODE_OPTIONS":"synthetic-offline-options"}, {"HOME":"synthetic-offline-home"}):
                path.write_text(json.dumps({"version":1,"offline_environment":fixtures,"groups":{"one":{"python":["test_probe"]}}}))
                with self.assertRaisesRegex(ValueError,"离线环境夹具"):
                    gate.read_groups(path,root)
            path.write_text(json.dumps({"version":1,"offline_environment":{"PACK_BASIC":"synthetic-offline-basic"},"groups":{"one":{"python":["test_probe"]}}}))
            self.assertEqual(gate.read_groups(path,root).offline_environment,{"PACK_BASIC":"synthetic-offline-basic"})

    def test_staged_plan_reads_staged_names_and_includes_deleted_rename_source(self):
        result=SimpleNamespace(stdout=b'old.py\0new.py\0')
        with patch.object(gate.subprocess,"run",return_value=result) as run:
            self.assertEqual(gate.changed_files(staged=True),["new.py","old.py"])
        self.assertIn("--cached",run.call_args.args[0])
        self.assertIn("--no-renames",run.call_args.args[0])
        run.assert_called_once()

    def test_known_document_gap_does_not_allow_a_new_missing_id(self):
        with tempfile.TemporaryDirectory() as directory:
            doc=Path(directory)/"sample.md"
            doc.write_text('sample')
            for ids,expected in (({"MKYC-R02"},0),({"MKYC-R02","NEW-R01"},1)):
                output=io.StringIO()
                with patch.object(docs,"DOC_DIR",Path(directory)), \
                     patch.object(docs,"collect_code_ids",return_value=({"sample":{"labels":["sample"],"ids":ids}},[])), \
                     patch("pack_registry.load_packs",return_value=[{"case_doc_exceptions":{"MKYC-R02":"已知历史差异"}}]), \
                     patch.object(sys,"argv",["check_case_doc_sync.py","--strict","--allow-known"]), \
                     contextlib.redirect_stdout(output):
                    if expected:
                        with self.assertRaises(SystemExit) as raised: docs.main()
                        self.assertEqual(raised.exception.code,expected)
                    else: docs.main()
                self.assertIn("保留的历史差异",output.getvalue())
                if expected:self.assertIn("NEW-R01",output.getvalue())
