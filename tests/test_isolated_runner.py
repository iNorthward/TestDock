import importlib.util,json,os,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'test-platform'),str(ROOT/'scripts')]
from export_platform import build
from create_project_pack import create
from isolated_runner import run_isolated
class IsolatedRunnerTests(unittest.TestCase):
    def test_hung_readonly_worker_is_stopped_and_next_case_works(self):
        with tempfile.TemporaryDirectory() as temp:
            output=build(Path(temp)/'release');root=output/'platform';create('hang_app','Synthetic',18808,root=root)
            source=root/'packs/hang_app/cases/readonly_cases.py';original=source.read_text()
            source.write_text(original+"\ndef _read_fixture(case):\n    import time\n    from pathlib import Path\n    p=Path('data/hang_app/entered');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('entered')\n    time.sleep(10)\n")
            started=time.monotonic();result=run_isolated('DEMO-R01',{},1.5,pack_id='hang_app',root=root)
            self.assertEqual(result['executionStatus'],'timed_out');self.assertIsNone(result['ok']);self.assertTrue(result['isolation']['terminated'])
            self.assertTrue((root/'data/hang_app/entered').exists());self.assertLess(time.monotonic()-started,4)
            source.write_text(original);result=run_isolated('DEMO-R01',{},3,pack_id='hang_app',root=root)
            self.assertEqual(result['status'],'passed');self.assertFalse(result['isolation']['terminated'])
    def test_invalid_timeout_rejected_before_spawn(self):
        for value in (True,0,float('nan'),4000):
            with self.assertRaises(ValueError):run_isolated('R01',{},value,pack_id='sample')
