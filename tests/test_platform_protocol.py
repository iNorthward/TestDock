import unittest
from platform_protocol import compatibility,default_requirement
class PlatformProtocolTests(unittest.TestCase):
    def test_supported_and_legacy_packs(self):
        self.assertTrue(compatibility({'platform_api':default_requirement()})['explicit'])
        self.assertFalse(compatibility({})['explicit'])
    def test_invalid_and_incompatible_are_rejected(self):
        for spec in ({'min':'2.0','max_exclusive':'3.0'}, {'min':'1.0','max_exclusive':'2.0','features':['foreign']},
                     {'min':True,'max_exclusive':'2.0'}, {'min':'1.0','max_exclusive':'1.0'},
                     {'min':'1.0','max_exclusive':'2.0','features':['request_id','request_id']}):
            with self.subTest(spec=spec),self.assertRaises(ValueError):compatibility({'platform_api':spec})
