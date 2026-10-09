from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

from db_reconcile_helpers import compare_totals, reconcile_aggregate  # noqa: E402


class DbReconcileHelpersTest(unittest.TestCase):
    def test_large_ids_and_identity_strings_compare_exactly(self):
        self.assertFalse(reconcile_aggregate(
            {"userId": 9007199254740992},
            {"user_id": 9007199254740993},
            {"userId": "user_id"},
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"idCard": "110101199001011234"},
            {"id_card": "110101199001011235"},
            {"idCard": "id_card"},
        )[0])

    def test_numeric_tolerance_remains_available(self):
        field_map = {"profit": "profit"}
        self.assertTrue(reconcile_aggregate(
            {"profit": "1.00009"}, {"profit": 1}, field_map,
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"profit": "1.00011"}, {"profit": 1}, field_map,
        )[0])

    def test_non_finite_values_never_reconcile(self):
        for value in (float("nan"), float("inf"), "-Infinity"):
            with self.subTest(value=value):
                self.assertFalse(reconcile_aggregate(
                    {"stat": value}, {"stat": value}, {"stat": "stat"},
                )[0])

    def test_money_fields_do_not_treat_two_missing_amounts_as_equal(self):
        self.assertTrue(reconcile_aggregate(
            {"optionalNote": None}, {"optional_note": None}, {"optionalNote": "optional_note"},
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"amount": None}, {"amount": None}, {"amount": "amount"},
            money_fields={"amount"},
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"amount": "NaN"}, {"amount": "NaN"}, {"amount": "amount"},
            money_fields={"amount"},
        )[0])

    def test_optional_money_fields_explicitly_allow_only_two_empty_values(self):
        kwargs = {
            "money_fields": {"serviceFee"},
            "optional_money_fields": {"serviceFee"},
        }
        self.assertTrue(reconcile_aggregate(
            {"serviceFee": None}, {"service_fee": None},
            {"serviceFee": "service_fee"}, **kwargs,
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"serviceFee": None}, {"service_fee": "0"},
            {"serviceFee": "service_fee"}, **kwargs,
        )[0])
        self.assertFalse(reconcile_aggregate(
            {"serviceFee": "NaN"}, {"service_fee": "NaN"},
            {"serviceFee": "service_fee"}, **kwargs,
        )[0])

    def test_totals_require_whole_nonnegative_numbers(self):
        self.assertTrue(compare_totals("12", 12)[0])
        self.assertTrue(compare_totals(12.0, 12)[0])
        for invalid in (None, True, -1, 1.9, float("nan"), float("inf"), "not-a-count"):
            with self.subTest(invalid=invalid):
                self.assertFalse(compare_totals(invalid, 1)[0])


if __name__ == "__main__":
    unittest.main()
