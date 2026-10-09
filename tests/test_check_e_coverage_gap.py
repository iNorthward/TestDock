import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_e_coverage_gap import analyze_gap, load_existing_e_cases  # noqa: E402


class CheckECoverageGapTests(unittest.TestCase):


    def test_summary_counts_only_candidate_endpoints_in_scope(self):
        result = analyze_gap(
            {"candidate": {"endpoint": "POST /mgr/user/client/changeStatus", "kind": "no_token"}},
            {
                "POST /mgr/user/client/changeStatus": [{"id": "CST-E01"}],
                "GET /unrelated": [{"id": "OTHER-E01"}],
            },
            detail=True,
        )

        self.assertEqual(result["summary"]["totalEndpoints"], 1)
        self.assertEqual(result["summary"]["endpointsWithECases"], 1)
        self.assertEqual(result["summary"]["endpointsNoECases"], 0)
        covered_endpoint = result["withECoverage"][0]
        self.assertEqual(covered_endpoint["candidateUniversalKinds"], ["no_token"])
        self.assertNotIn("missingUniversalKinds", covered_endpoint)


if __name__ == "__main__":
    unittest.main()
