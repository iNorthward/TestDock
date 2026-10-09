"""Integration contract for generated projects and neutral releases."""
import importlib.util,json,shutil,shlex,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/str(name+'.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
targeted=script('targeted_regression')
create=script('create_project_pack');verify=script('verify_project_pack');neutral=script('check_neutral_distribution')
class AgentOnboardingTests(unittest.TestCase):
    def test_new_project_runs_without_core_edits_or_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for folder in ('test-platform','examples','resources','scripts'):shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            for filename in ('platform_config.py','timezone_config.py','platform_protocol.py'):shutil.copyfile(ROOT/filename,root/filename)
            before=(root/'test-platform/catalog.py').read_bytes()
            create.create('sensor_app','传感器',18802,root=root)
            result=verify.verify('sensor_app',smoke='DEMO-R01',root=root)
            self.assertEqual(result['pack'],'sensor_app');self.assertEqual(result['mockPassed'],'DEMO-R01')
            self.assertEqual((root/'test-platform/catalog.py').read_bytes(),before)
            self.assertFalse((root/'data').exists())
            with self.assertRaises(ValueError):create.create('sensor_app','覆盖',18803,root=root)
    def test_invalid_project_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as temp:
            for identifier in ('../foreign','UPPER','demo_pack','two-projects','class'):
                with self.assertRaises(ValueError):create.create(identifier,'测试',8802,root=temp)
            self.assertEqual(list(Path(temp).iterdir()),[])
    def test_residue_and_broken_links_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);(p/'README.md').write_text('m'+'lt'+'\n[missing](absent.md)')
            found=neutral.findings(p);self.assertEqual({f['kind'] for f in found},{'legacy_identity','broken_local_link'})
            (p/'README.md').write_text('generic platform');self.assertEqual(neutral.findings(p),[])
            (p/('m'+'lt_hidden.py')).write_text('');self.assertEqual(neutral.findings(p)[0]['kind'],'legacy_filename')

    def test_pack_regression_accepts_owned_tests_and_rejects_foreign_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            shutil.copytree(ROOT/'examples',root/'examples')
            create.create('sensor_app','Sensors',18802,root=root)
            manifest=root/'packs/sensor_app/regression.json'
            groups=targeted.read_groups(manifest,root=root,pack_id='sensor_app')
            groups.pack_id='sensor_app'
            plan=targeted.build_plan(groups,['packs/sensor_app/tests/test_adapter.py'],root=root)
            self.assertEqual(plan['python'],['packs.sensor_app.tests.test_adapter'])
            self.assertEqual(plan['unmappedSources'],[])
            with self.assertRaises(ValueError):targeted.read_groups(manifest,root=root,pack_id='other_app')
            foreign=root/'packs/other_app/tests';foreign.mkdir(parents=True)
            (foreign/'test_adapter.py').write_text('')
            owned=root/'packs/sensor_app/tests/test_adapter.py';owned.unlink();owned.symlink_to(foreign/'test_adapter.py')
            with self.assertRaises(ValueError):targeted.read_groups(manifest,root=root,pack_id='sensor_app')

    def test_http_starter_accepts_explicit_contract_and_runs_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for folder in ('test-platform','examples','resources','scripts'):
                shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('platform_config.py','timezone_config.py','platform_protocol.py'):shutil.copyfile(ROOT/name,root/name)
            recipe=root/'examples/http_pack_template/contract.example.json'
            create.create('http_app','HTTP App',18803,root=root,contract_file=recipe)
            readme=(root/'packs/http_app/README.md').read_text()
            command=shlex.split(next(line for line in readme.splitlines() if line.startswith('python3 scripts/verify_project_pack.py')))
            documented_pack=command[command.index('--pack')+1]
            documented_case=command[command.index('--case-id')+1]
            result=verify.verify(documented_pack,smoke=documented_case,root=root)
            self.assertIn('PLATFORM_ENV_FILE=.env.http_app ./start.sh start',readme)
            self.assertNotIn('DEMO-R01',readme)
            self.assertEqual(result['mockPassed'],'HTTP-MOCK-R01')
            self.assertEqual(result['cases'],2)
            self.assertEqual(result['warnings'],[])
            generated=(root/'packs/http_app/integration.py').read_text()
            self.assertIn("'ok':None,'status':'incomplete'",generated)
            self.assertIn('SERVICE_ACCESS_TOKEN=',(root/'.env.http_app').read_text())

    def test_http_starter_rejects_credentials_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'contract.json'
            value=json.loads((ROOT/'examples/http_pack_template/contract.example.json').read_text())
            value['headers']['Authorization']['literal']='not-a-real-secret'
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):create.http_contract(path)
            del value['headers']['Authorization']['literal'];value['path']='//foreign.invalid/ready'
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):create.http_contract(path)

    def test_pack_export_contains_only_owned_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'release'
            script('export_platform').build(output,pack_ids=['demo_pack'])
            bundle=output/'pack-demo_pack'
            self.assertTrue((bundle/'packs/demo_pack/pack.json').exists())
            self.assertTrue((bundle/'examples/demo_pack/readonly_cases.py').exists())
            self.assertFalse((bundle/'tests').exists())
            self.assertFalse((bundle/'.env.local').exists())
            self.assertFalse((bundle/'.git').exists())
            platform=output/'platform'
            self.assertEqual((platform/'README.md').read_text(),(ROOT/'README.md').read_text())
            self.assertTrue((platform/'docs/onboarding/QUICKSTART.md').exists())
            self.assertEqual(script('check_neutral_distribution').findings(platform),[])

    def test_http_recipe_cannot_override_platform_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'contract.json'
            original=json.loads((ROOT/'examples/http_pack_template/contract.example.json').read_text())
            for key in ('PLATFORM_PACKS','PLATFORM_ENV_FILE','PLATFORM_PANEL_PORT',
                        'PLATFORM_PACK_DATA_DIR','PLATFORM_ARTIFACT_ROOT','TZ'):
                for location in ('base','auth'):
                    value=json.loads(json.dumps(original))
                    if location=='base':value['base_env']=key
                    else:value['headers']['Authorization']['env']=key
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        create.create('collision_app','Collision',8802,root=root,contract_file=path)
                    self.assertFalse((root/'packs').exists())
                    self.assertFalse((root/'.env.collision_app').exists())
