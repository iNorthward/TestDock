import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TaskEvidenceRecipeTests(unittest.TestCase):
    def test_isolated_rounds_preserve_failure_unknown_write_and_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "example"
            completed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "examples/task_evidence/demo.py"), "--output", str(output)],
                env={**os.environ, "PLATFORM_ENV_FILE": os.devnull}, capture_output=True, text=True, timeout=40,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads((output / "summary.json").read_bytes())
            self.assertIs(summary["ok"], True)
            self.assertIs(summary["realBusinessVerified"], False)
            self.assertEqual(summary["changedInputEvidenceStatus"], "stale")
            runs = summary["runs"]
            self.assertEqual(len({run["runId"] for run in runs.values()}), 4)
            self.assertEqual({name: row["status"] for name, row in runs.items()},
                             {"success": "passed", "failed": "failed", "cancelled": "incomplete", "lost_response": "incomplete"})

            success = output / runs["success"]["runId"]
            self.assertIsNone(json.loads((success / "first-report.json").read_bytes())["cleanup"]["ok"])
            self.assertIsNone(json.loads((success / "cleanup-intent.json").read_bytes())["ok"])
            interrupted = json.loads((success / "cleanup-interrupted-report.json").read_bytes())
            self.assertIsNone(interrupted["cleanup"]["ok"])
            self.assertIsNone(interrupted["ok"])
            self.assertTrue(json.loads((success / "cleanup.json").read_bytes())["verifiedAbsent"])
            self.assertFalse(json.loads((success / "cleanup.json").read_bytes())["removedThisAttempt"])
            self.assertFalse(json.loads((success / "cleanup-repeat.json").read_bytes())["removedThisAttempt"])
            self.assertFalse((success / "import.json").exists())
            self.assertIn("import.json", runs["success"]["evidence"])
            failed = output / runs["failed"]["runId"]
            self.assertIn("synthetic import rejected", (failed / "failure.log").read_text())
            self.assertLessEqual((failed / "details.log").stat().st_size, 256)
            self.assertTrue(runs["failed"]["detailLogTruncated"])
            self.assertIsNone(runs["failed"]["phases"][1]["ok"])
            self.assertEqual(runs["cancelled"]["receipt"]["status"], "cancelled")
            self.assertFalse((output / runs["cancelled"]["runId"] / "import.json").exists())

            unknown = runs["lost_response"]
            directory = output / unknown["runId"]
            before = json.loads((directory / "first-report.json").read_bytes())
            self.assertEqual(unknown["receipt"], before["receipt"])
            self.assertEqual(unknown["receipt"]["state"], "active")
            self.assertIsNone(unknown["phases"][0]["ok"])
            self.assertIs(unknown["phases"][1]["ok"], True)
            self.assertIsNone(unknown["cleanup"]["ok"])
            self.assertEqual(before["evidence"]["import.json"], hashlib.sha256((directory / "import.json").read_bytes()).hexdigest())
            self.assertEqual(hashlib.sha256((directory / "input.json").read_bytes()).hexdigest(), unknown["inputSha256"])
            with sqlite3.connect(directory / "execution_jobs.db") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0], 1)
            database = output / "history/run_history.db"
            with sqlite3.connect(database) as connection:
                actual = connection.execute("SELECT status FROM runs ORDER BY id").fetchall()
                self.assertEqual(actual, [("passed",), ("failed",), ("incomplete",), ("incomplete",)])
