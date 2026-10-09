import unittest
from packs.PACK_IDENTIFIER.integration import CONTRACT,inspect_response,run_probe
class ContractTests(unittest.TestCase):
    def test_declared_mock_contract(self):
        result=run_probe(mock=True);self.assertEqual(result['status'],'passed')
    def test_wrong_http_status_is_failure(self):
        response=CONTRACT['mock_response'];self.assertTrue(inspect_response(599,response['body']))
    def test_missing_nested_fields_fail(self):
        self.assertTrue(inspect_response(CONTRACT['expected_status'],None))
