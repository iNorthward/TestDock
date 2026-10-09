from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_apifox_integrated as wrapper  # noqa: E402


class SyncApifoxIntegratedTests(unittest.TestCase):
    def test_forwards_all_supported_options_and_propagates_exit_code(self):
        argv = [
            "sync_apifox_integrated.py", "--json", "--skip-fetch", "--skip-safe",
            "--skip-safe-reason=ui", "--advance-baseline",
        ]
        with patch.object(sys, "argv", argv), \
                patch.object(wrapper.subprocess, "call", return_value=7) as call:
            with self.assertRaises(SystemExit) as raised:
                wrapper.main()

        self.assertEqual(raised.exception.code, 7)
        call.assert_called_once_with(
            [sys.executable, str(wrapper.INSPECT), *argv[1:]],
            cwd=str(wrapper.ROOT),
        )

    def test_does_not_silently_discard_unknown_options(self):
        argv = ["sync_apifox_integrated.py", "--unknown-option"]
        with patch.object(sys, "argv", argv), \
                patch.object(wrapper.subprocess, "call", return_value=2) as call:
            with self.assertRaises(SystemExit) as raised:
                wrapper.main()

        self.assertEqual(raised.exception.code, 2)
        forwarded = call.call_args.args[0]
        self.assertEqual(forwarded[-1], "--unknown-option")


if __name__ == "__main__":
    unittest.main()
