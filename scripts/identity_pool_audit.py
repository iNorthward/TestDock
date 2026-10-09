#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计账号池静态缺口；apply-safe 默认仅预览，写入须 --apply --confirm。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TP = ROOT / "test-platform"
sys.path.insert(0, str(TP))

import identity_pool
import identity_pool_audit_core as auditlib


def _print_audit(report: dict) -> None:
    summary = report["summary"]
    print("Pack: %s · 扫描 Python 文件 %s 个" % (report["pack_id"], summary["files_scanned"]))
    print("缺口：角色 %s、场景 %s、夹具 %s；已有记录缺字段/引用 %s；重名 %s" % (
        summary["missing_roles"], summary["missing_scenarios"], summary["missing_fixtures"],
        summary["incomplete_entries"], summary["duplicate_names"],
    ))
    for title, key in (("缺少角色", "missing_roles"), ("缺少正向场景", "missing_scenarios"),
                       ("缺少负向夹具", "missing_fixtures"), ("已有记录不完整", "incomplete_entries"),
                       ("重复名称", "duplicate_names")):
        rows = report.get(key) or []
        if not rows:
            continue
        print("\n%s：" % title)
        for row in rows:
            name = row.get("name") or "%s.%s" % (row.get("category"), row.get("role"))
            title_text = row.get("title") or name
            print("- %s [%s]" % (title_text, name))
            print("  是什么 / 用途：%s" % (row.get("purpose") or "使用位置待配置"))
            print("  需要什么：%s" % auditlib._needs_text(row.get("needs") or {}))
            print("  建议：%s" % (row.get("suggestion") or "需人工检查"))
            origins = []
            for label, key in (("用例", "cases"), ("套件", "suites"), ("文件", "sources"), ("记录类型", "collections")):
                values = row.get(key) or []
                if values:
                    origins.append("%s %s" % (label, "、".join(str(value) for value in values)))
            if origins:
                print("  来源：%s" % "；".join(origins))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "apply-safe"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--pack", default=None)
        if name == "audit":
            cmd.add_argument("--json", action="store_true", help="将 JSON 写到标准输出")
            cmd.add_argument("--output", type=Path, help="将审计 JSON 写入指定文件")
        else:
            cmd.add_argument("--apply", action="store_true", help="实际写入身份池；默认 dry-run")
            cmd.add_argument("--confirm", action="store_true", help="确认只执行决策表 1/2 安全变更")
    args = parser.parse_args(argv)

    try:
        if args.pack is None:
            from pack_registry import load_packs
            args.pack = load_packs()[0]["id"]
        report, pack, pool, scan = auditlib.audit_root(ROOT, args.pack)
    except Exception as exc:
        print("审计失败：%s" % exc, file=sys.stderr)
        return 2

    if args.command == "audit":
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_audit(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("JSON 已写入：%s" % args.output)
        return 0

    changes = auditlib.plan_safe_changes(pack=pack, pool=pool, audit=report, code_scan=scan)
    if args.apply and not args.confirm:
        print("拒绝写入：实际写入必须同时提供 --apply --confirm。", file=sys.stderr)
        return 2
    updated, applied = auditlib.apply_changes(pool, changes)
    print("apply-safe %s" % ("写入" if args.apply else "dry-run"))
    if changes["append_roles"]:
        print("可追加的 CLI roles：%s" % ", ".join(item["role"] for item in changes["append_roles"]))
    if changes["add_fixtures"]:
        print("可建立的负向 fixtures：%s" % ", ".join(item["name"] for item in changes["add_fixtures"]))
    if not changes["append_roles"] and not changes["add_fixtures"]:
        print("没有符合决策表 1/2 的安全变更。")
    if args.apply:
        if applied:
            identity_pool.save_pool(updated, pack_id=args.pack)
            for item in applied:
                print("已应用：%s" % json.dumps(item, ensure_ascii=False))
        else:
            print("没有写入变更。")
    else:
        print("预览未写入；使用 --apply --confirm 才会修改池文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
