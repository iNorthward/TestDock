from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform" / "common"))

from money_cmp import money_diff, money_eq, to_decimal  # noqa: E402


class MoneyCmpTest(unittest.TestCase):
    def test_half_up_rounding(self):
        self.assertEqual(to_decimal("1.00005").quantize(Decimal("0.0001"), rounding="ROUND_HALF_UP"), Decimal("1.0001"))
        self.assertTrue(money_eq("1.00005", "1.0001", places=4)[0])

    def test_unequal_values_include_quantized_detail(self):
        ok, detail = money_eq("1", "1.0002")
        self.assertFalse(ok)
        self.assertIn("1.0000", detail)
        self.assertIn("1.0002", detail)

    def test_null_handling(self):
        self.assertFalse(money_eq(None, "0")[0])
        self.assertTrue(money_eq(None, None, allow_both_null=True)[0])

    def test_to_decimal(self):
        self.assertEqual(to_decimal("1,234.50"), Decimal("1234.50"))
        self.assertIsNone(to_decimal(""))

    def test_money_diff_default_precision(self):
        self.assertEqual(money_diff("2", "0.5"), Decimal("1.5000"))

    def test_non_finite_and_unquantizable_amounts_fail_closed(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e1000000"):
            self.assertFalse(money_eq(value, value, allow_both_null=True)[0])
            self.assertIsNone(money_diff(value, value))
        self.assertIsNone(to_decimal(Decimal("NaN")))
        ok, detail = money_eq("Infinity", "1")
        self.assertFalse(ok)
        self.assertIn("无效", detail)
        self.assertNotIn("为空", detail)
        empty_ok, empty_detail = money_eq(None, "1")
        self.assertFalse(empty_ok)
        self.assertIn("为空", empty_detail)


if __name__ == "__main__":
    unittest.main()
