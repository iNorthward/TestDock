from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test-platform"))

from insights_actions import build_action_sections  # noqa: E402


class InsightsActionsTest(unittest.TestCase):
    def test_groups_uncovered_paths_candidate_gaps_and_recent_case_reasons(self):
        result = build_action_sections(
            {
                "generatedAt": "2026-09-27",
                "paths": [
                    {"method": "GET", "path": "/cli/not-covered", "covered": False,
                     "summaryLabel": "cli · 未覆盖", "apifoxModule": "用户"},
                    {"method": "GET", "path": "/cli/candidate", "covered": True,
                     "apifoxModule": "客户用户"},
                    {"method": "GET", "path": "/cli/covered", "covered": True},
                ],
            },
            {
                "R": {"expectedEndpoints": 3, "realizedEndpoints": 2,
                      "topGaps": [("GET /cli/candidate", 4)]},
                "F": {"expectedEndpoints": 1, "realizedEndpoints": 1, "topGaps": []},
            },
            [
                {"id": 12, "ts": 200, "status": "failed", "failed": 1, "source": "nightly"},
                {"id": 13, "ts": 300, "status": "skipped", "skipped": 1, "source": "panel"},
                {"id": 11, "ts": 100, "status": "passed", "failed": 0},
            ],
            {
                12: {"cases": [
                    {"id": "N-R01", "status": "failed", "subs": [
                        {"status": "failed", "label": "断言", "detail": "返回值不匹配"},
                    ]},
                    {"id": "N-R02", "status": "incomplete", "subs": [
                        {"status": "incomplete", "label": "fixture", "detail": "未配置测试样本"},
                    ]},
                    {"id": "N-R03", "status": "passed", "subs": []},
                    {"id": "N-R04", "status": "skipped", "subs": [
                        {"status": "skipped", "label": "可选筛选", "detail": "已按配置跳过"},
                    ]},
                ]},
                13: {"cases": [
                    {"id": "N-S01", "status": "skipped", "subs": [
                        {"status": "skipped", "label": "条件不满足", "detail": "缺少隔离样本"},
                    ]},
                ]},
            },
        )

        self.assertEqual(result["coverage"]["uncoveredCount"], 1)
        self.assertEqual(result["coverage"]["items"][0]["path"], "/cli/not-covered")
        self.assertEqual(result["coverage"]["items"][0]["module"], "用户")
        self.assertEqual(result["candidates"]["expectedEndpoints"], 4)
        self.assertEqual(result["candidates"]["realizedEndpoints"], 3)
        self.assertTrue(result["candidates"]["available"])
        candidate = result["candidates"]["groups"][0]["items"][0]
        self.assertEqual((candidate["series"], candidate["endpoint"], candidate["candidateCount"]),
                         ("R", "GET /cli/candidate", 4))
        self.assertEqual(candidate["module"], "客户用户")
        self.assertEqual(result["history"]["runsWithIssues"], 2)
        self.assertEqual([item["caseId"] for item in result["history"]["items"]],
                         ["N-S01", "N-R01", "N-R02", "N-R04"])
        self.assertEqual(result["history"]["items"][0]["reasonType"], "已跳过（查看原因）")
        self.assertIn("缺少隔离样本", result["history"]["items"][0]["reason"])
        self.assertEqual(result["history"]["items"][1]["reasonType"], "真失败")
        self.assertIn("返回值不匹配", result["history"]["items"][1]["reason"])
        self.assertIn("未配置测试样本", result["history"]["items"][2]["reason"])
        self.assertEqual(result["history"]["items"][3]["reasonType"], "已跳过（查看原因）")
        self.assertIn("已按配置跳过", result["history"]["items"][3]["reason"])

    def test_action_data_is_bounded_and_empty_sources_remain_valid(self):
        result = build_action_sections(
            {"paths": [{"method": "GET", "path": f"/u/{i}", "covered": False} for i in range(5)]},
            {"R": {"expectedEndpoints": 0, "realizedEndpoints": 0,
                   "topGaps": [(f"GET /c/{i}", i) for i in range(5)]}},
            [],
            {},
            limits={"coverage": 2, "candidate_per_series": 3},
        )
        self.assertEqual(result["coverage"]["uncoveredCount"], 5)
        self.assertEqual(len(result["coverage"]["items"]), 2)
        self.assertEqual(result["candidates"]["gapCount"], 5)
        self.assertEqual(len(result["candidates"]["groups"][0]["items"]), 3)
        self.assertEqual(result["history"], {"runsWithIssues": 0, "items": []})

    def test_server_and_insights_page_are_wired_to_the_action_payload(self):
        server = (ROOT / "test-platform" / "server.py").read_text(encoding="utf-8")
        page = (ROOT / "test-platform" / "insights.html").read_text(encoding="utf-8")
        self.assertIn('p.path == "/api/insights/actions"', server)
        self.assertIn("build_action_sections(coverage, scorecard, issue_runs, run_details)", server)
        self.assertIn("api('/api/insights/actions')", page)
        self.assertIn("renderInsightActions(stack,actionData)", page)
        self.assertIn("候选未落地", page)
        self.assertIn("复制 Agent 提示", page)

    def test_partial_source_failures_keep_repair_hints_visible(self):
        server = (ROOT / "test-platform" / "server.py").read_text(encoding="utf-8")
        page = (ROOT / "test-platform" / "insights.html").read_text(encoding="utf-8")
        self.assertIn("覆盖索引不可用；配置 PLATFORM_PACK_DATA_DIR 后运行 python3 scripts/gen_apifox_coverage_wiki.py 生成。", server)
        self.assertIn("候选评分卡不可用", server)
        self.assertIn("运行历史暂不可用", server)
        self.assertIn("candidate.available?'候选评分卡中没有零用例端点。':'候选评分卡暂不可用", page)
        self.assertIn("actions&&actions.warnings||[]", page)
        self.assertIn("warn.textContent=w", page)
        self.assertIn("行动清单暂不可用；可查看下方覆盖视图与回归历史。", page)

    def test_recent_issues_precede_module_collapsed_gap_lists(self):
        page = (ROOT / "test-platform" / "insights.html").read_text(encoding="utf-8")
        recent = page.index("addGroup(`近期失败 / 跳过 / 未完成 ·")
        uncovered = page.index("addGroup(`未覆盖接口 ·")
        candidates = page.index("addGroup(`候选未落地 ·")
        self.assertLess(recent, uncovered)
        self.assertLess(uncovered, candidates)
        self.assertIn("el('details','ins-action-module')", page)
        self.assertIn("summary.textContent=`${name} · ${rows.length} 条`", page)
        self.assertGreaterEqual(page.count("{collapseByModule:true}"), 2)
        self.assertNotIn("details.open=true", page)
        coverage_loader = page[
            page.index("async function loadCoverage(){"):page.index("async function loadEnvPreflight(){")
        ]
        self.assertLess(
            coverage_loader.index("api('/api/insights/actions')"),
            coverage_loader.index("if(coverageError)"),
        )

    def test_optional_capability_empty_states_name_missing_items_and_next_commands(self):
        page = (ROOT / "test-platform" / "insights.html").read_text(encoding="utf-8")
        self.assertIn("缺少配置名：${apifoxMissingConfig.join('、')}", page)
        self.assertIn("python3 scripts/run_apifox_inspection.py", page)
        self.assertIn("缺少配置名 PLATFORM_PACK_DATA_DIR", page)
        self.assertIn("python3 scripts/gen_apifox_coverage_wiki.py", page)
        self.assertIn("缺少配置：${[...new Set(missingOps)].join('、')}", page)
        self.assertIn("python3 scripts/error_log_audit.py", page)
        self.assertIn("核心面板仍可正常使用", page)


if __name__ == "__main__":
    unittest.main()
