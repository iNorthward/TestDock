import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))

from pack_registry import _read_candidates, panel_pack_summaries  # noqa: E402


class PanelPackLabelTests(unittest.TestCase):
    def test_pack_manifests_expose_names_for_panel_headers(self):
        packs = {pack["id"]: pack for pack in _read_candidates()}

        self.assertEqual(packs["demo_pack"]["display_name"], "demo")
        self.assertEqual(
            panel_pack_summaries([packs["demo_pack"]]),
            [{"id": "demo_pack", "name": "demo"}],
        )

    def test_panel_summary_uses_only_selected_pack_names(self):
        summaries = panel_pack_summaries([
            {"id": "demo_pack", "display_name": "demo"},
        ])

        self.assertEqual(summaries, [{"id": "demo_pack", "name": "demo"}])


if __name__ == "__main__":
    unittest.main()
