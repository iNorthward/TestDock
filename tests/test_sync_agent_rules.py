import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_agent_rules as rules


class SyncAgentRulesTests(unittest.TestCase):
    def _isolated_rule_dirs(self, root):
        src = root / "agent-rules"
        cursor = root / ".cursor" / "rules"
        claude = root / ".claude" / "rules"
        for directory in (src, cursor, claude):
            directory.mkdir(parents=True)
        return src, cursor, claude

    def test_check_does_not_bootstrap_or_create_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "agent-rules"
            cursor = root / ".cursor" / "rules"
            claude = root / ".claude" / "rules"
            cursor.mkdir(parents=True)
            cursor_file = cursor / "demo.mdc"
            cursor_text = rules.render_cursor(rules._as_rule(always_apply=True, body="demo"))
            cursor_file.write_text(cursor_text, encoding="utf-8")

            with patch.object(rules, "ROOT", root), \
                 patch.object(rules, "SRC_DIR", src), \
                 patch.object(rules, "CURSOR_DIR", cursor), \
                 patch.object(rules, "CLAUDE_DIR", claude), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(rules.sync(check=True), 1)
                self.assertFalse(src.exists())
                self.assertFalse(claude.exists())
                self.assertEqual(cursor_file.read_text(encoding="utf-8"), cursor_text)

                self.assertEqual(rules.sync(check=False), 0)
                self.assertTrue((src / "demo.md").is_file())
                self.assertTrue((claude / "demo.md").is_file())

    def test_delete_removes_exact_rule_from_all_three_locations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, cursor, claude = self._isolated_rule_dirs(root)
            targets = (src / "demo.md", cursor / "demo.mdc", claude / "demo.md")
            neighbors = (src / "keep.md", cursor / "keep.mdc", claude / "keep.md")
            for path in (*targets, *neighbors):
                path.write_text("rule", encoding="utf-8")

            with patch.object(rules, "ROOT", root), \
                 patch.object(rules, "SRC_DIR", src), \
                 patch.object(rules, "CURSOR_DIR", cursor), \
                 patch.object(rules, "CLAUDE_DIR", claude), \
                 patch.object(sys, "argv", ["sync_agent_rules.py", "--delete", "demo"]), \
                 redirect_stdout(io.StringIO()):
                rules.main()

            self.assertTrue(all(not path.exists() for path in targets))
            self.assertTrue(all(path.exists() for path in neighbors))

    def test_delete_rejects_path_traversal_and_readme(self):
        for stem in ("../demo", "nested/rule", "README", ".hidden", "bad.name"):
            with self.subTest(stem=stem), self.assertRaises(ValueError):
                rules.delete_rule(stem)

    def test_delete_preflights_all_targets_before_removing_any(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, cursor, claude = self._isolated_rule_dirs(root)
            source = src / "demo.md"
            source.write_text("rule", encoding="utf-8")
            (cursor / "demo.mdc").mkdir()

            with patch.object(rules, "SRC_DIR", src), \
                 patch.object(rules, "CURSOR_DIR", cursor), \
                 patch.object(rules, "CLAUDE_DIR", claude), \
                 self.assertRaises(ValueError):
                rules.delete_rule("demo")

            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
