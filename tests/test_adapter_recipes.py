import unittest
from concurrent.futures import ThreadPoolExecutor
from examples.adapter_recipes.auth_cache import CachedLoginTokens
from examples.adapter_recipes.contracts import ConfiguredEnvelope,PageContract
class AdapterRecipeTests(unittest.TestCase):
    def test_expiry_identity_and_singleflight(self):
        calls=[];now=[0]
        def login(user,tenant):calls.append((user,tenant));return {'access_token':'synthetic-token-'+str(len(calls)),'expires_in':20}
        provider=CachedLoginTokens(login,max_ttl=30,refresh_margin=2,clock=lambda:now[0])
        with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(lambda _:provider.get_token('u','t'),range(8)))
        self.assertEqual(len(set(values)),1);self.assertEqual(len(calls),1)
        now[0]=19;self.assertNotEqual(provider.get_token('u','t'),values[0])
        provider.get_token('u','other');self.assertEqual(len(calls),3)
        provider.invalidate('u','t');provider.get_token('u','t');self.assertEqual(len(calls),4)
    def test_login_failure_does_not_use_expired_cache(self):
        now=[0];calls=[0]
        def login(*args):
            calls[0]+=1
            if calls[0]>1:raise ValueError('synthetic login failure')
            return {'access_token':'synthetic-token','expires_in':3}
        provider=CachedLoginTokens(login,max_ttl=5,refresh_margin=1,clock=lambda:now[0]);provider.get_token('u','t');now[0]=3
        with self.assertRaises(ValueError):provider.get_token('u','t')
    def test_envelope_and_page_keep_raw_json_types(self):
        envelope=ConfiguredEnvelope(success_path=['meta','code'],success_value=1,data_path=['payload'],code_path=['meta','code'],message_path=['meta','message'])
        self.assertTrue(envelope.is_ok({'meta':{'code':1},'payload':{}}))
        self.assertFalse(envelope.is_ok({'meta':{'code':True},'payload':{}}))
        self.assertFalse(envelope.validate_envelope({'meta':{'code':1}})[0])
        page=PageContract(records_path=['payload','items'],total_path=['payload','count'])
        self.assertEqual(page.validate({'payload':{'items':[{}],'count':2}}),[])
        for total in (True,'2',0,-1):self.assertTrue(page.validate({'payload':{'items':[{}],'count':total}}))
