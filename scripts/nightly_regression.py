#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时回归：跑全量 SAFE → 落库 run_history → 失败时 webhook 通知。

设计为无 GUI、无外部依赖，可挂 cron / launchd 定时执行：
  # 每天 02:30 跑一次（crontab -e）
  30 2 * * * cd $PLATFORM_ROOT && ./scripts/run_nightly.sh >> .run/nightly.log 2>&1

通知渠道（可选，二选一或都配）：
  PLATFORM_WEBHOOK_WECHAT  企业微信机器人 webhook URL
  PLATFORM_WEBHOOK_DINGTALK 钉钉机器人 webhook URL
无配置时仅落库并打印结果。

用法：
  python3 scripts/nightly_regression.py                 # 全量 SAFE
  python3 scripts/nightly_regression.py --suite notice  # 指定 suite
  python3 scripts/nightly_regression.py --no-notify
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from timezone_config import configure_timezone  # noqa: E402

configure_timezone()
TP = ROOT / "test-platform"
for sub in ("", "common"):
    sys.path.insert(0, str(TP / sub))

from case_result_status import normalize_case_result, summarize_cases


def _post_json(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as resp:
        return resp.status


def notify(text: str):
    sent = []
    wechat = os.environ.get("PLATFORM_WEBHOOK_WECHAT", "").strip()
    dingtalk = os.environ.get("PLATFORM_WEBHOOK_DINGTALK", "").strip()
    if wechat:
        try:
            status = _post_json(wechat, {"msgtype": "text", "text": {"content": text}})
            if type(status) is not int or not 200 <= status < 300:
                raise RuntimeError("webhook returned a non-2xx HTTP status")
            sent.append("wechat")
        except Exception as e:
            # The webhook URL is a credential and urllib exceptions may echo it.
            print(f"[notify] wechat 失败: {type(e).__name__}")
    if dingtalk:
        try:
            status = _post_json(dingtalk, {"msgtype": "text", "text": {"content": text}})
            if type(status) is not int or not 200 <= status < 300:
                raise RuntimeError("webhook returned a non-2xx HTTP status")
            sent.append("dingtalk")
        except Exception as e:
            # Keep the channel diagnostic without printing the secret URL or error payload.
            print(f"[notify] dingtalk 失败: {type(e).__name__}")
    return sent


def _validated_run_result(result, *, suite):
    """Reject summary/case drift before recording or reporting a nightly run."""
    if not isinstance(result, dict) or not isinstance(result.get("cases"), list):
        return None, None, "runner 未返回 cases 列表"
    if result.get("mode") != "safe" or result.get("suite") != suite:
        return None, None, "runner 返回的 mode/suite 与请求不一致"

    cases = [normalize_case_result(c) for c in result["cases"]]
    counts = summarize_cases(cases)
    for key in ("total", "passed", "failed", "skipped", "incomplete"):
        value = result.get(key)
        if type(value) is not int or value != counts[key]:
            return None, None, f"runner 汇总字段 {key} 与 cases 不一致"
    if result.get("status") != counts["status"]:
        return None, None, "runner 汇总字段 status 与 cases 不一致"
    return cases, counts, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default=None)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    import catalog as cat
    import run_history as rh

    if args.suite and args.suite not in {s["id"] for s in cat.SUITES}:
        print(f"未知 suite：{args.suite}", file=sys.stderr)
        sys.exit(2)

    t0 = time.monotonic()
    result = cat.run_all(mode="safe", suite=args.suite)
    dur_ms = int((time.monotonic() - t0) * 1000)
    cases, counts, result_error = _validated_run_result(result, suite=args.suite)
    if result_error:
        print(f"回归结果不一致：{result_error}，未写入运行历史。", file=sys.stderr)
        sys.exit(2)
    if counts["total"] == 0:
        scope = f"suite {args.suite}" if args.suite else "全量回归"
        print(f"{scope} 没有 SAFE 用例，未写入运行历史。", file=sys.stderr)
        sys.exit(2)
    run_id = rh.record_run(result, source="nightly", duration_ms=dur_ms)

    total = counts["total"]
    failed = [c.get("id", "?") for c in cases if c.get("status") == "failed"]
    skipped = [c.get("id", "?") for c in cases if c.get("status") == "skipped"]
    incomplete = [c.get("id", "?") for c in cases if c.get("status") == "incomplete"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (f"PLATFORM 回归 #{run_id} [{stamp}] SAFE 通过 {counts['passed']}/{total}，"
            f"失败 {counts['failed']}，跳过 {counts['skipped']}，未完成 {counts['incomplete']}，"
            f"耗时 {dur_ms/1000:.1f}s")
    print(head)

    exit_status = 1 if failed else 2 if skipped or incomplete else 0
    if exit_status:
        details = [head]
        for label, ids in (("失败", failed), ("跳过", skipped), ("未完成", incomplete)):
            if ids:
                details.append(f"{label} {len(ids)} 条：\n" + "\n".join(f"  - {i}" for i in ids[:30]))
                if len(ids) > 30:
                    details.append(f"  … {label}另有 {len(ids) - 30} 条")
        detail = "\n".join(details)
        print(detail)
        if not args.no_notify:
            sent = notify(detail)
            if sent:
                print(f"[notify] 已收到 HTTP 2xx 响应: {', '.join(sent)}（应用层回执未核验）")
        sys.exit(exit_status)
    else:
        print("全部通过。")
        if not args.no_notify and os.environ.get("PLATFORM_NOTIFY_ON_SUCCESS"):
            notify(head + " ✅ 全绿")


if __name__ == "__main__":
    main()
