import importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/str(name+'.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
class ProjectHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        pack=self.root/'packs/sample';pack.mkdir(parents=True);(pack/'pack.json').write_text(json.dumps({'id':'sample'}))
        self.tasks=script('project_tasks')
    def evidence(self,value):
        p=self.root/'evidence.json';p.write_text(json.dumps(value));return p
    def test_scope_requires_evidence_and_changed_file_invalidates_handoff(self):
        self.assertFalse(self.tasks.init('sample',root=self.root,require_real=True)['ok'])
        p=self.evidence({'pack':'sample','authorizedReadOnly':True,'caseIds':['R01']})
        d=self.tasks.record('sample','scope',p,root=self.root)
        self.assertEqual(d['tasks'][0]['status'],'passed')
        p.write_text('{}');self.assertEqual(self.tasks.status('sample',root=self.root)['tasks'][0]['status'],'stale')
    def test_foreign_pack_or_false_success_evidence_is_rejected(self):
        for value in ({'pack':'other','authorizedReadOnly':True,'caseIds':['R01']},{'pack':'sample','authorizedReadOnly':False,'caseIds':['R01']}):
            with self.assertRaises(ValueError):self.tasks.record('sample','scope',self.evidence(value),root=self.root)
        self.assertFalse((self.root/'data').exists())
    def test_real_case_must_be_in_authorized_scope(self):
        p=self.evidence({'pack':'sample','authorizedReadOnly':True,'caseIds':['R01']})
        self.tasks.record('sample','scope',p,root=self.root)
        other=self.root/'real.json';other.write_text(json.dumps({'pack':'sample','ok':True,'scope':'real_readonly','realBusinessVerified':True,'caseIds':['R02']}))
        with self.assertRaises(ValueError):self.tasks.record('sample','real_readonly',other,root=self.root)
    def test_state_cannot_escape_through_a_symlink(self):
        (self.root/'data').mkdir();(self.root/'data/sample').symlink_to(self.root.parent)
        with self.assertRaises(ValueError):self.tasks.init('sample',root=self.root)
    def test_doctor_checks_configuration_without_exposing_values(self):
        doctor=script('project_doctor')
        pack=self.root/'packs/sample';(pack/'pack.json').write_text(json.dumps({'id':'sample','config_defaults':{'PLATFORM_PANEL_PORT':'8802','PLATFORM_PACK_DATA_DIR':'data/sample','PLATFORM_ARTIFACT_ROOT':'artifacts/sample'}}))
        (pack/'http-contract.json').write_text(json.dumps({'base_env':'SERVICE_URL','headers':{'Authorization':{'env':'ACCESS_TOKEN'}}}))
        (self.root/'.env.sample').write_text('PLATFORM_PACKS=sample\nSERVICE_URL=https://synthetic.invalid\nACCESS_TOKEN=synthetic-private-value\n')
        original=doctor.verify;doctor.verify=lambda *a,**k:{'cases':1}
        self.addCleanup(setattr,doctor,'verify',original)
        d=doctor.diagnose('sample',root=self.root,mock_case='R01')
        self.assertEqual(d['status'],'ready');self.assertFalse(d['realBusinessVerified'])
        self.assertNotIn('synthetic-private-value',json.dumps(d))
        (self.root/'.env.sample').write_text('PLATFORM_PACKS=sample\nSERVICE_URL=https://synthetic.invalid\n')
        self.assertEqual(doctor.diagnose('sample',root=self.root)['status'],'incomplete')

    def test_malformed_manifest_settings_are_reported_without_crashing(self):
        doctor=script('project_doctor')
        (self.root/'packs/sample/pack.json').write_text(json.dumps({'id':'sample','config_defaults':None,'required_environment':['lowercase',True]}))
        original=doctor.verify;doctor.verify=lambda *a,**k:{'cases':1}
        self.addCleanup(setattr,doctor,'verify',original)
        result=doctor.diagnose('sample',root=self.root)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(sum(c['name']=='manifest' and c['status']=='failed' for c in result['checks']),2)
