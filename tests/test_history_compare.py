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

    def test_missing_contract_files_do_not_count_as_available_evidence(self):
        a=self.run_data(fingerprint=None);b=self.run_data(fingerprint=None)
        self.assertFalse(compare_runs(a,b)['contractEvidenceAvailable'])
        a['executionMetadata']['contractFingerprints']={'http':'digest','missing':None}
        b['executionMetadata']['contractFingerprints']={'http':'digest'}
        self.assertFalse(compare_runs(a,b)['contractEvidenceAvailable'])

    def test_malformed_history_containers_raise_validation_errors(self):
        for field,value in (('executionMetadata',None),('executionMetadata',[]),('cases',None)):
            a=self.run_data();a[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):compare_runs(a,self.run_data())

    def test_runtime_snapshot_and_legacy_disk_fingerprint_are_not_mixed(self):
        a=self.run_data();b=self.run_data()
        b['executionMetadata']['codeEvidence']={'basis':'catalog-load-source-snapshot','status':'captured'}
        d=compare_runs(a,b)
        self.assertIsNone(d['codeChanged'])
        self.assertFalse(d['codeEvidenceAvailable'])
        a['executionMetadata']['codeEvidence']={'basis':'catalog-load-source-snapshot','status':'captured'}
        a['executionMetadata']['diskCodeFingerprint']='disk-v1'
        b['executionMetadata']['diskCodeFingerprint']='disk-v2'
        a['executionMetadata']['codeDrift']=False
        b['executionMetadata']['codeDrift']=True
        d=compare_runs(a,b)
        self.assertFalse(d['codeChanged'])
        self.assertTrue(d['diskCodeChanged'])
        self.assertTrue(d['afterCodeDrift'])
        self.assertTrue(d['codeEvidenceAvailable'])
        b['executionMetadata']['codeEvidence']['status']='unknown'
        self.assertIsNone(compare_runs(a,b)['codeChanged'])
