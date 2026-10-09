import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'test-platform'))
from history_compare import compare_runs
class HistoryCompareTests(unittest.TestCase):
    def run_data(self,status='passed',fingerprint='a'):
        return {'id':1,'executionMetadata':{'packId':'sample','codeFingerprint':'code','contractFingerprints':{'http':fingerprint}},'cases':[{'id':'R01','status':status,'subs':[{'label':'type','ok':status=='passed','detail':'shape'}]}]}
    def test_status_assertion_and_contract_changes(self):
        d=compare_runs(self.run_data(),self.run_data('failed','b'))
        self.assertEqual(d['changes'][0]['afterStatus'],'failed');self.assertEqual(len(d['changes'][0]['assertionChanges']),1)
        self.assertEqual(d['contractChanges'][0]['after'],'b')
    def test_added_removed_and_unchanged(self):
        a=self.run_data();b=self.run_data();self.assertEqual(compare_runs(a,b)['unchangedCases'],1)
        b['cases'][0]['id']='R02';self.assertEqual({r['kind'] for r in compare_runs(a,b)['changes']},{'added','removed'})
    def test_missing_evidence_and_cross_pack_are_not_assumed(self):
        a=self.run_data();b=self.run_data();a['executionMetadata']={}
        self.assertFalse(compare_runs(a,b)['contractEvidenceAvailable']);self.assertIsNone(compare_runs(a,b)['codeChanged'])
        a=self.run_data();b['executionMetadata']['packId']='foreign'
        with self.assertRaises(ValueError):compare_runs(a,b)
    def test_duplicate_assertion_labels_are_preserved(self):
        a=self.run_data();b=self.run_data();a['cases'][0]['subs']*=2;b['cases'][0]['subs']=[{'label':'type','ok':True,'detail':'shape'},{'label':'type','ok':False,'detail':'changed'}]
        changes=compare_runs(a,b)['changes'][0]['assertionChanges'];self.assertEqual(changes[0]['occurrence'],2)
